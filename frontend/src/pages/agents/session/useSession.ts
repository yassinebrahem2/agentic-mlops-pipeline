import { useMutation } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, unwrap } from "@/lib/api";
import { queryClient } from "@/lib/query";
import { useProjectEvents } from "@/lib/useProjectEvents";
import { applyEvent, type SessionState } from "./applyEvent";
import { eventNames, parseEvent } from "./events";
import { isConnecting, openStream, StreamBackoff, type StreamLike } from "@/lib/stream";

export type UseSessionResult = {
  state: SessionState | undefined;
  error: ApiError | undefined;
  connection: "live" | "reconnecting" | "closed";
  send(text: string): Promise<void>;
  stop(): Promise<void>;
  reload(): Promise<void>;
};

function isRefusal(status: number | undefined): boolean {
  return status === 404 || status === 401;
}

export function useSession(projectId: string, agent: string): UseSessionResult {
  const [state, setState] = useState<SessionState>();
  const [error, setError] = useState<ApiError>();
  const [connection, setConnection] = useState<"live" | "reconnecting" | "closed">("closed");

  const streamRef = useRef<StreamLike | undefined>(undefined);
  const backoffRef = useRef(new StreamBackoff());
  const stateRef = useRef<SessionState | undefined>(undefined);

  const reloadRef = useRef<() => Promise<void>>(() => Promise.resolve());
  const refreshTaskRef = useRef<() => void>(() => {});

  // Task events come on the project stream, not the session stream. Refreshing the snapshot on
  // them keeps it the one source of the current task, shown from its start to its end.
  useProjectEvents(projectId, {
    onEvent: (event) => {
      if (
        (event.type === "task.started" || event.type === "task.ended") &&
        event.source === agent
      ) {
        refreshTaskRef.current();
      }
    },
    // A resync means task events may have been missed.
    onResync: () => {
      refreshTaskRef.current();
    },
  });

  const sendMutation = useMutation({
    mutationFn: async (text: string) => {
      const result = await api.POST("/api/projects/{project_id}/agents/{agent}/session/messages", {
        params: { path: { project_id: projectId, agent } },
        body: { text },
      });
      unwrap(result);
    },
  });

  const stopMutation = useMutation({
    mutationFn: async () => {
      const result = await api.POST("/api/projects/{project_id}/agents/{agent}/session/stop", {
        params: { path: { project_id: projectId, agent } },
      });
      unwrap(result);
    },
  });

  useEffect(() => {
    const backoff = backoffRef.current;
    backoff.reset();
    stateRef.current = undefined;
    setState(undefined);
    setError(undefined);
    setConnection("closed");

    if (!projectId || !agent) {
      return;
    }

    let disposed = false;
    let taskRefreshSeq = 0;
    let appliedTaskRefreshSeq = 0;

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
        current_task: session.current_task,
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
          appliedTaskRefreshSeq = ++taskRefreshSeq;
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
        }
        return { error: apiErr };
      }
    };

    const refreshTask = () => {
      const seq = ++taskRefreshSeq;
      void fetchSessionSnapshot()
        .then((snapshot) => {
          if (!disposed && stateRef.current && seq > appliedTaskRefreshSeq) {
            appliedTaskRefreshSeq = seq;
            const updated: SessionState = {
              ...stateRef.current,
              current_task: snapshot.current_task,
            };
            stateRef.current = updated;
            queryClient.setQueryData(queryKey, updated);
            setState(updated);
          }
        })
        .catch((error: unknown) => {
          console.error("Session task refresh failed", error);
        });
    };
    refreshTaskRef.current = refreshTask;

    const schedule = () => {
      backoffRef.current.schedule(() => {
        void connect(true);
      });
    };

    const handleStreamError = () => {
      close();
      setConnection("reconnecting");
      void load().then((result) => {
        if (disposed) return;
        if (isRefusal(result.error?.status)) {
          setConnection("closed");
          return;
        }
        setConnection("reconnecting");
        schedule();
      });
    };

    const connect = async (reloadSession = false) => {
      let loaded: SessionState | undefined;
      if (reloadSession) {
        setConnection("reconnecting");
        const result = await load();
        if (disposed) return;
        if (!result.state) {
          if (isRefusal(result.error?.status)) {
            setConnection("closed");
            return;
          }
          setConnection("reconnecting");
          schedule();
          return;
        }
        loaded = result.state;
      } else {
        const result = stateRef.current ? { state: stateRef.current } : await load();
        if (disposed || !result.state) return;
        loaded = result.state;
      }

      close();

      const url = `/api/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agent)}/session/stream?after=${String(loaded.lastSeq)}`;
      const stream = openStream(url);
      streamRef.current = stream;

      stream.addEventListener("open", () => {
        if (!disposed) {
          setConnection("live");
          backoffRef.current.reset();
        }
      });

      const handle = (event: Event) => {
        const message = event as MessageEvent<string>;
        const parsed = parseEvent(event.type, message.data);
        const seq = Number(message.lastEventId);

        if (!parsed || !Number.isSafeInteger(seq)) {
          handleStreamError();
          return;
        }

        if (!disposed) {
          setConnection("live");
          backoffRef.current.reset();
        }

        if (parsed.type === "resync") {
          close();
          setConnection("reconnecting");
          void load().then((result) => {
            if (disposed) return;
            if (result.state) {
              void connect(false);
            } else if (isRefusal(result.error?.status)) {
              setConnection("closed");
            } else {
              setConnection("reconnecting");
              schedule();
            }
          });
          return;
        }

        const current = stateRef.current;
        if (!current) return;
        const next = applyEvent(current, { seq, ...parsed });
        stateRef.current = next;
        queryClient.setQueryData(queryKey, next);
        setState(next);

        if (parsed.type === "turn.ended") {
          refreshTask();
        }
      };

      for (const name of eventNames) {
        stream.addEventListener(name, handle);
      }

      stream.onerror = () => {
        if (disposed) return;
        if (isConnecting(stream)) {
          setConnection("reconnecting");
          return;
        }
        handleStreamError();
      };
    };

    reloadRef.current = async () => {
      backoffRef.current.reset();
      close();
      const result = await load();
      if (result.state && !disposed) {
        void connect(false);
      } else if (!disposed) {
        setConnection("closed");
      }
    };

    void load().then((result) => {
      if (result.state && !disposed) {
        void connect(false);
      } else if (!disposed) {
        setConnection("closed");
      }
    });

    return () => {
      disposed = true;
      backoff.clear();
      close();
    };
  }, [projectId, agent]);

  const send = useCallback(
    async (text: string) => {
      await sendMutation.mutateAsync(text);
    },
    [sendMutation],
  );

  const stop = useCallback(async () => {
    await stopMutation.mutateAsync();
  }, [stopMutation]);

  const reload = useCallback(async () => {
    await reloadRef.current();
  }, []);

  return { state, error, connection, send, stop, reload };
}
