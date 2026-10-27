import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi, type MockInstance } from "vitest";
import { queryClient } from "@/lib/query";
import * as streamModule from "@/lib/stream";
import { STREAM_CLOSED, STREAM_OPEN, type StreamLike } from "./stream";

export { STREAM_CLOSED, STREAM_CONNECTING, STREAM_OPEN, type StreamLike } from "./stream";

export const initialSession = {
  id: "s1",
  agent: "orchestrator",
  busy: false,
  messages: [],
  last_seq: 4,
};

export class FakeStream implements StreamLike {
  url: string;
  readyState = STREAM_OPEN;
  onerror: ((event: Event) => void) | null = null;
  listeners = new Map<string, (event: Event) => void>();

  constructor(url: string = "") {
    this.url = url;
  }

  addEventListener(type: string, listener: (event: Event) => void): void {
    this.listeners.set(type, listener);
  }

  removeEventListener(type: string, listener: (event: Event) => void): void {
    if (this.listeners.get(type) === listener) {
      this.listeners.delete(type);
    }
  }

  close = vi.fn(() => {
    this.readyState = STREAM_CLOSED;
  });

  emitOpen(): void {
    this.listeners.get("open")?.(new Event("open"));
  }

  emit(type: string, data: unknown, seq?: number): void {
    const event = {
      type,
      data: typeof data === "string" ? data : JSON.stringify(data),
      lastEventId: seq !== undefined ? String(seq) : undefined,
    } as unknown as MessageEvent;
    this.listeners.get(type)?.(event);
  }
}

export const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);

/** Session streams go to `createdStreams` and project event streams to `eventStreams`, so a
 * test counts and drives each kind on its own. */
export function setupSessionStreamMock(
  createdStreams: FakeStream[],
  eventStreams: FakeStream[] = [],
): MockInstance {
  queryClient.clear();
  return vi.spyOn(streamModule, "openStream").mockImplementation((url: string) => {
    const stream = new FakeStream(url);
    (url.includes("/session/stream") ? createdStreams : eventStreams).push(stream);
    return stream;
  });
}
