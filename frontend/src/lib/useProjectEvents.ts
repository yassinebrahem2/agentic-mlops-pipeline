import { useEffect, useRef, useState } from "react";
import type { components } from "@/api/schema";
import { isConnecting, openStream, StreamBackoff, type StreamLike } from "@/lib/stream";

export type EventRecord = components["schemas"]["EventRecord"];
export type ProjectEventType = EventRecord["type"];

export const PROJECT_EVENT_TYPES: readonly ProjectEventType[] = [
  "turn.started",
  "turn.waiting",
  "turn.ended",
  "task.started",
  "task.ended",
  "pr.opened",
  "pr.updated",
  "issue.opened",
  "issue.commented",
  "issue.updated",
  "approval.requested",
  "approval.decided",
  "message.sent",
  "wakeup.scheduled",
  "sandbox.build.started",
  "sandbox.build.finished",
  "sandbox.build.failed",
] satisfies readonly ProjectEventType[];

export type UseProjectEventsOptions = {
  onEvent?: (event: EventRecord) => void;
  onResync?: () => void;
};

export type UseProjectEventsResult = {
  connection: "live" | "reconnecting" | "closed";
};

/**
 * Subscribes to the project event stream over SSE (/api/projects/{projectId}/events/stream),
 * using the shared reconnect backoff and stream lifecycle logic.
 */
export function useProjectEvents(
  projectId: string | undefined,
  options?: UseProjectEventsOptions,
): UseProjectEventsResult {
  const [connection, setConnection] = useState<"live" | "reconnecting" | "closed">("closed");

  const streamRef = useRef<StreamLike | undefined>(undefined);
  const backoffRef = useRef(new StreamBackoff());
  const lastSeqRef = useRef<number | undefined>(undefined);

  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    const backoff = backoffRef.current;
    backoff.reset();
    lastSeqRef.current = undefined;
    setConnection("closed");

    if (!projectId) {
      return;
    }

    let disposed = false;

    const close = () => {
      streamRef.current?.close();
      streamRef.current = undefined;
    };

    const handleStreamError = () => {
      close();
      setConnection("reconnecting");
      backoff.schedule(() => {
        if (!disposed) {
          connect();
        }
      });
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
          backoff.reset();
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
          backoff.reset();
        }

        if (e.type === "resync") {
          lastSeqRef.current = undefined;
          optionsRef.current?.onResync?.();
          return;
        }

        try {
          const parsed = JSON.parse(message.data) as EventRecord;
          optionsRef.current?.onEvent?.(parsed);
        } catch {
          handleStreamError();
        }
      };

      for (const name of [...PROJECT_EVENT_TYPES, "resync", "message"]) {
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

    connect();

    return () => {
      disposed = true;
      backoff.clear();
      close();
    };
  }, [projectId]);

  return { connection };
}
