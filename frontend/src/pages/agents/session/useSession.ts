import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, unwrap } from "../../../lib/api";
import { queryClient } from "../../../lib/query";
import { applyEvent, type SessionState } from "./applyEvent";
import { parseEvent, type SessionView } from "./events";
import { openStream, STREAM_CONNECTING, type StreamLike } from "./stream";

type ApiResult = { data?: unknown; error?: unknown; response: Response };
type SessionApi = {
  GET(path: string): Promise<ApiResult>;
  POST(path: string, options: { body: unknown }): Promise<ApiResult>;
};
type LoadResult = { state?: SessionState; error?: ApiError };

const sessionApi = api as unknown as SessionApi;
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
};

function sessionPath(projectId: string, agent: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agent)}/session`;
}

function asSession(value: unknown): SessionView {
  return value as SessionView;
}

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
  const stateRef = useRef<SessionState | undefined>(undefined);
  const errorRef = useRef<ApiError | undefined>(undefined);
  const open = options?.openStream ?? openStream;
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    stateRef.current = undefined;
    errorRef.current = undefined;
    setState(undefined);
    setError(undefined);
    setConnection("closed");
    const close = () => {
      streamRef.current?.close();
      streamRef.current = undefined;
    };
    const load = async (): Promise<LoadResult> => {
      try {
        const result = await sessionApi.GET(sessionPath(projectId, agent));
        const session = asSession(unwrap(result));
        const next = { messages: session.messages, busy: session.busy, lastSeq: session.last_seq };
        if (!disposed) {
          stateRef.current = next;
          setState(next);
          errorRef.current = undefined;
          setError(undefined);
        }
        return { state: next };
      } catch (caught) {
        if (!disposed && caught instanceof ApiError) {
          errorRef.current = caught;
          setError(caught);
          if (caught.status === 401) void queryClient.invalidateQueries({ queryKey: ["auth"] });
        }
        return { error: caught instanceof ApiError ? caught : undefined };
      }
    };
    const schedule = () => {
      const delay = retryRef.current < 3 ? [1000, 2000, 5000][retryRef.current] : 10000;
      retryRef.current += 1;
      retryTimer = setTimeout(() => void connect(true), delay);
    };
    const connect = async (reload = false) => {
      const loaded = reload ? (await load()).state : (stateRef.current ?? (await load()).state);
      if (disposed || !loaded) return;
      close();
      const url = `${sessionPath(projectId, agent)}/stream?after=${String(loaded.lastSeq)}`;
      const stream = openRef.current(url);
      streamRef.current = stream;
      setConnection("live");
      stream.addEventListener("open", () => {
        if (!disposed) setConnection("live");
      });
      const handle = (event: Event) => {
        const message = event as MessageEvent<string>;
        const parsed = parseEvent(event.type, message.data);
        const seq = Number(message.lastEventId);
        if (!parsed || !Number.isSafeInteger(seq)) return;
        setConnection("live");
        retryRef.current = 0;
        if (parsed.type === "resync") {
          close();
          void load().then((result) => {
            if (result.state && !disposed) void connect();
          });
          return;
        }
        setState((current) => {
          if (!current) return current;
          const next = applyEvent(current, { seq, type: parsed.type, data: parsed });
          stateRef.current = next;
          return next;
        });
      };
      for (const name of eventNames) stream.addEventListener(name, handle);
      stream.onerror = () => {
        if (disposed) return;
        if (stream.readyState === STREAM_CONNECTING) {
          setConnection("reconnecting");
          return;
        }
        close();
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
    void load().then((result) => {
      if (result.state) void connect();
    });
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      close();
    };
  }, [projectId, agent]);

  const send = useCallback(
    async (text: string) => {
      try {
        const result = await sessionApi.POST(`${sessionPath(projectId, agent)}/messages`, {
          body: { text },
        });
        unwrap(result);
      } catch (caught) {
        if (caught instanceof ApiError) {
          setError(caught);
          if (caught.status === 401) void queryClient.invalidateQueries({ queryKey: ["auth"] });
        }
        throw caught;
      }
    },
    [projectId, agent],
  );

  const stop = useCallback(async () => {
    const result = await sessionApi.POST(`${sessionPath(projectId, agent)}/stop`, {
      body: undefined,
    });
    unwrap(result);
  }, [projectId, agent]);

  return { state, error, connection, send, stop };
}
