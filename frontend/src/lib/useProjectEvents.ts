import { useEffect, useRef, useState } from "react";
import type { components } from "@/api/schema";
import {
  isConnecting,
  openStream,
  StreamBackoff,
  type StreamConnectionState,
  type StreamLike,
} from "@/lib/stream";

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
  connection: StreamConnectionState;
};

type Subscriber = {
  onEvent: (event: EventRecord) => void;
  onResync: () => void;
  onConnection: (state: StreamConnectionState) => void;
};

/**
 * One SSE connection to a project's event stream, shared by every subscriber of that project:
 * browsers allow only ~6 HTTP/1.1 connections per host, and a page can hold many hooks.
 */
class SharedStream {
  private readonly projectId: string;
  private readonly backoff = new StreamBackoff();
  private readonly subscribers = new Set<Subscriber>();
  private stream: StreamLike | undefined;
  private lastSeq: number | undefined;
  private disposed = false;
  private state: StreamConnectionState = "closed";

  constructor(projectId: string) {
    this.projectId = projectId;
  }

  get connection(): StreamConnectionState {
    return this.state;
  }

  add(subscriber: Subscriber): void {
    this.subscribers.add(subscriber);
  }

  /** Removes the subscriber; returns true when none is left. */
  remove(subscriber: Subscriber): boolean {
    this.subscribers.delete(subscriber);
    return this.subscribers.size === 0;
  }

  dispose(): void {
    this.disposed = true;
    this.backoff.clear();
    this.close();
  }

  connect(): void {
    this.close();

    const after = this.lastSeq !== undefined ? `?after=${String(this.lastSeq)}` : "";
    const url = `/api/projects/${encodeURIComponent(this.projectId)}/events/stream${after}`;
    const stream = openStream(url);
    this.stream = stream;

    stream.addEventListener("open", () => {
      if (!this.disposed) {
        this.setConnection("live");
        this.backoff.reset();
      }
    });

    const handle = (e: Event) => {
      this.handleMessage(e);
    };
    for (const name of [...PROJECT_EVENT_TYPES, "resync", "message"]) {
      stream.addEventListener(name, handle);
    }

    stream.onerror = () => {
      if (this.disposed) return;
      if (isConnecting(stream)) {
        this.setConnection("reconnecting");
        return;
      }
      this.handleStreamError();
    };
  }

  private handleMessage(e: Event): void {
    const message = e as MessageEvent<string>;
    const seq = Number(message.lastEventId);
    if (Number.isSafeInteger(seq)) {
      this.lastSeq = seq;
    }

    if (!this.disposed) {
      this.setConnection("live");
      this.backoff.reset();
    }

    if (e.type === "resync") {
      this.lastSeq = undefined;
      this.notify((subscriber) => {
        subscriber.onResync();
      });
      return;
    }

    let parsed: EventRecord;
    try {
      parsed = JSON.parse(message.data) as EventRecord;
    } catch {
      this.handleStreamError();
      return;
    }
    this.notify((subscriber) => {
      subscriber.onEvent(parsed);
    });
  }

  private close(): void {
    this.stream?.close();
    this.stream = undefined;
  }

  private handleStreamError(): void {
    this.close();
    this.setConnection("reconnecting");
    this.backoff.schedule(() => {
      if (!this.disposed) {
        this.connect();
      }
    });
  }

  private setConnection(state: StreamConnectionState): void {
    if (this.state === state) return;
    this.state = state;
    this.notify((subscriber) => {
      subscriber.onConnection(state);
    });
  }

  /**
   * Calls every subscriber in isolation: one consumer's bug must neither stop delivery to the
   * others nor tear down the shared connection, so its error is logged instead of propagated.
   */
  private notify(call: (subscriber: Subscriber) => void): void {
    for (const subscriber of [...this.subscribers]) {
      if (!this.subscribers.has(subscriber)) continue;
      try {
        call(subscriber);
      } catch (error) {
        console.error("Project event subscriber failed", error);
      }
    }
  }
}

const sharedStreams = new Map<string, SharedStream>();

function subscribe(projectId: string, subscriber: Subscriber): () => void {
  const existing = sharedStreams.get(projectId);
  const shared = existing ?? new SharedStream(projectId);
  shared.add(subscriber);
  subscriber.onConnection(shared.connection);
  if (!existing) {
    sharedStreams.set(projectId, shared);
    shared.connect();
  }

  let subscribed = true;
  return () => {
    if (!subscribed) return;
    subscribed = false;
    if (shared.remove(subscriber)) {
      shared.dispose();
      sharedStreams.delete(projectId);
    }
  };
}

/**
 * Subscribes to the project event stream over SSE (/api/projects/{projectId}/events/stream).
 * All subscribers of a project share one connection, opened on the first and closed on the last.
 */
export function useProjectEvents(
  projectId: string | undefined,
  options?: UseProjectEventsOptions,
): UseProjectEventsResult {
  const [connection, setConnection] = useState<StreamConnectionState>("closed");

  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    if (!projectId) {
      setConnection("closed");
      return;
    }

    return subscribe(projectId, {
      onEvent: (event) => {
        optionsRef.current?.onEvent?.(event);
      },
      onResync: () => {
        optionsRef.current?.onResync?.();
      },
      onConnection: setConnection,
    });
  }, [projectId]);

  return { connection };
}
