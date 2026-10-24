import { useEffect, useRef, useState } from "react";
import type { components } from "@/api/schema";
import { openStream, STREAM_CONNECTING, type StreamLike } from "@/pages/agents/session/stream";

export type EventRecord = components["schemas"]["EventRecord"];

export const PROJECT_EVENT_TYPES = [
  "turn.started",
  "turn.waiting",
  "turn.ended",
  "task.started",
  "task.ended",
  "pr.opened",
  "pr.updated",
  "issue.opened",
  "issue.commented",
  "approval.requested",
  "approval.decided",
  "message.sent",
  "wakeup.scheduled",
  "sandbox.build.started",
  "sandbox.build.finished",
  "sandbox.build.failed",
  "resync",
] as const;

export type UseProjectEventsOptions = {
  onEvent?: (event: EventRecord) => void;
  onResync?: () => void;
};

export type UseProjectEventsResult = {
  connection: "live" | "reconnecting" | "closed";
  error: Error | undefined;
};

/**
 * Subscribes to the project event stream over SSE (/api/projects/{projectId}/events/stream),
 * reusing the reconnect backoff and stream lifecycle logic.
 */
export function useProjectEvents(
  projectId: string | undefined,
  handlerOrOptions?: ((event: EventRecord) => void) | UseProjectEventsOptions,
): UseProjectEventsResult {
  const [connection, setConnection] = useState<"live" | "reconnecting" | "closed">("closed");
  const [error, setError] = useState<Error | undefined>(undefined);

  const streamRef = useRef<StreamLike | undefined>(undefined);
  const retryRef = useRef(0);
  const lastSeqRef = useRef<number | undefined>(undefined);

  const handlerRef = useRef(handlerOrOptions);
  handlerRef.current = handlerOrOptions;

  useEffect(() => {
    retryRef.current = 0;
    lastSeqRef.current = undefined;
    setConnection("closed");
    setError(undefined);

    if (!projectId) {
      return;
    }

    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    const close = () => {
      streamRef.current?.close();
      streamRef.current = undefined;
    };

    const schedule = () => {
      const delays = [1000, 2000, 5000];
      const delay = retryRef.current < delays.length ? delays[retryRef.current] : 10000;
      retryRef.current += 1;
      retryTimer = setTimeout(() => {
        connect();
      }, delay);
    };

    const connect = () => {
      close();

      const after = lastSeqRef.current !== undefined ? `?after=${String(lastSeqRef.current)}` : "";
      const url = `/api/projects/${encodeURIComponent(projectId)}/events/stream${after}`;
      const stream = openStream(url);
      streamRef.current = stream;

      stream.addEventListener("open", () => {
        if (!disposed) {
          setConnection("live");
          setError(undefined);
          retryRef.current = 0;
        }
      });

      const handle = (e: Event) => {
        const message = e as MessageEvent<string>;
        const seq = Number(message.lastEventId);
        if (Number.isSafeInteger(seq)) {
          lastSeqRef.current = seq;
        }

        if (!disposed) {
          setConnection("live");
          retryRef.current = 0;
        }

        if (e.type === "resync") {
          lastSeqRef.current = undefined;
          const opts = handlerRef.current;
          if (typeof opts === "object" && opts.onResync) {
            opts.onResync();
          }
          return;
        }

        try {
          const parsed = JSON.parse(message.data) as EventRecord;
          const opts = handlerRef.current;
          if (typeof opts === "function") {
            opts(parsed);
          } else if (typeof opts === "object" && opts.onEvent) {
            opts.onEvent(parsed);
          }
        } catch {
          // ignore invalid JSON
        }
      };

      for (const name of [...PROJECT_EVENT_TYPES, "message"]) {
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
        schedule();
      };
    };

    connect();

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      close();
    };
  }, [projectId]);

  return { connection, error };
}
