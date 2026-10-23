import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, unwrap } from "@/lib/api";
import { queryClient } from "@/lib/query";
import { applyEvent, type SessionState } from "./applyEvent";
import { parseEvent } from "./events";
import { openStream, STREAM_CONNECTING, type StreamLike } from "./stream";

const eventNames = [
  "message.created",
  "text.delta",
  "tool.started",
  "tool.finished",
  "message.updated",
  "turn.ended",
  "resync",
];

export type UseSessionResult = {
  state: SessionState | undefined;
  error: ApiError | undefined;
  connection: "live" | "reconnecting" | "closed";
  send(text: string): Promise<void>;
  stop(): Promise<void>;
  reload(): Promise<void>;
};

export function useSession(
  projectId: string,
  agent: string,
  options?: { openStream?: (url: string) => StreamLike },
): UseSessionResult {
  const [state, setState] = useState<SessionState>();
  const [error, setError] = useState<ApiError>();
  const [connection, setConnection] = useState<"live" | "reconnecting" | "closed">("closed");

  const streamRef = useRef<StreamLike | undefined>(undefined);
  const retryRef = useRef(0);
  const streamErrorsRef = useRef(0);
  const stateRef = useRef<SessionState | undefined>(undefined);
  const open = options?.openStream ?? openStream;
  const openRef = useRef(open);
  openRef.current = open;

  const reloadRef = useRef<() => Promise<void>>(() => Promise.resolve());

  useEffect(() => {
    retryRef.current = 0;
    streamErrorsRef.current = 0;
    stateRef.current = undefined;
    setState(undefined);
    setError(undefined);
    setConnection("closed");

    if (!projectId || !agent) {
      return;
    }

    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    const queryKey = ["session", projectId, agent] as const;

    const close = () => {
      streamRef.current?.close();
      streamRef.current = undefined;
    };

    const fetchSessionSnapshot = async (): Promise<SessionState> => {
      const result = await api.GET("/api/projects/{project_id}/agents/{agent}/session", {
        params: { path: { project_id: projectId, agent } },
      });
      const session = unwrap(result);
      return {
        messages: session.messages,
        busy: session.busy,
        lastSeq: session.last_seq,
      };
    };

    const load = async (): Promise<{ state?: SessionState; error?: ApiError }> => {
      try {
        const next = await queryClient.query({
          queryKey,
          queryFn: fetchSessionSnapshot,
          staleTime: 0,
          retry: false,
        });
        if (!disposed) {
          stateRef.current = next;
          setState(next);
          setError(undefined);
        }
        return { state: next };
      } catch (caught) {
        const apiErr =
          caught instanceof ApiError
            ? caught
            : new ApiError(
                "load_failed",
                caught instanceof Error ? caught.message : "Failed to load session",
                500,
                caught,
              );
        if (!disposed) {
          setError(apiErr);
          setConnection("closed");
        }
        return { error: apiErr };
      }
    };

    const schedule = () => {
      const delays = [1000, 2000, 5000];
      const delay = retryRef.current < delays.length ? delays[retryRef.current] : 10000;
      retryRef.current += 1;
      retryTimer = setTimeout(() => {
        void connect(true);
      }, delay);
    };

    const connect = async (reloadSession = false) => {
      const loaded = reloadSession
        ? (await load()).state
        : (stateRef.current ?? (await load()).state);
      if (disposed || !loaded) return;

      close();

      const url = `/api/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agent)}/session/stream?after=${String(loaded.lastSeq)}`;
      const stream = openRef.current(url);
      streamRef.current = stream;

      stream.addEventListener("open", () => {
        if (!disposed) {
          setConnection("live");
          retryRef.current = 0;
        }
      });

      const handle = (event: Event) => {
        const message = event as MessageEvent<string>;
        const parsed = parseEvent(event.type, message.data);
        const seq = Number(message.lastEventId);

        if (!parsed || !Number.isSafeInteger(seq)) {
          streamErrorsRef.current += 1;
          return;
        }

        if (!disposed) {
          setConnection("live");
          retryRef.current = 0;
        }

        if (parsed.type === "resync") {
          close();
          setConnection("reconnecting");
          void load().then((result) => {
            if (disposed) return;
            if (result.state) {
              void connect(false);
            } else {
              setConnection("closed");
            }
          });
          return;
        }

        setState((current) => {
          if (!current) return current;
          const next = applyEvent(current, { seq, type: parsed.type, data: parsed });
          stateRef.current = next;
          queryClient.setQueryData(queryKey, next);
          return next;
        });
      };

      for (const name of eventNames) {
        stream.addEventListener(name, handle);
      }

      stream.onerror = () => {
        if (disposed) return;
        if (stream.readyState === STREAM_CONNECTING) {
          setConnection("reconnecting");
          return;
        }

        close();
        setConnection("reconnecting");
        void load().then((result) => {
          if (disposed) return;
          if (result.error?.status === 404 || result.error?.status === 401) {
            setConnection("closed");
            return;
          }
          schedule();
        });
      };
    };

    reloadRef.current = async () => {
      if (retryTimer) clearTimeout(retryTimer);
      retryRef.current = 0;
      close();
      const result = await load();
      if (result.state && !disposed) {
        void connect(false);
      }
    };

    void load().then((result) => {
      if (result.state && !disposed) {
        void connect(false);
      }
    });

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      close();
    };
  }, [projectId, agent]);

  const send = useCallback(
    async (text: string) => {
      const result = await api.POST("/api/projects/{project_id}/agents/{agent}/session/messages", {
        params: { path: { project_id: projectId, agent } },
        body: { text },
      });
      unwrap(result);
    },
    [projectId, agent],
  );

  const stop = useCallback(async () => {
    const result = await api.POST("/api/projects/{project_id}/agents/{agent}/session/stop", {
      params: { path: { project_id: projectId, agent } },
    });
    unwrap(result);
  }, [projectId, agent]);

  const reload = useCallback(async () => {
    await reloadRef.current();
  }, []);

  return { state, error, connection, send, stop, reload };
}
