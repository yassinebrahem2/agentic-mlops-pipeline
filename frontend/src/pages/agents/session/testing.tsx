import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi, type MockInstance } from "vitest";
import { queryClient } from "@/lib/query";
import * as streamModule from "./stream";
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
  allListeners = new Map<string, Set<(event: Event) => void>>();

  constructor(url: string = "") {
    this.url = url;
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    if (typeof listener === "function") {
      this.listeners.set(type, listener);
      let set = this.allListeners.get(type);
      if (!set) {
        set = new Set();
        this.allListeners.set(type, set);
      }
      set.add(listener);
    }
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    if (typeof listener === "function") {
      if (this.listeners.get(type) === listener) {
        this.listeners.delete(type);
      }
      this.allListeners.get(type)?.delete(listener);
    }
  }

  close = vi.fn(() => {
    this.readyState = STREAM_CLOSED;
  });

  emitOpen(): void {
    const set = this.allListeners.get("open");
    if (set && set.size > 0) {
      set.forEach((fn) => {
        fn(new Event("open"));
      });
    } else {
      this.listeners.get("open")?.(new Event("open"));
    }
  }

  emit(type: string, data: unknown, seq: number): void {
    const event = {
      type,
      data: JSON.stringify(data),
      lastEventId: String(seq),
    } as MessageEvent;
    const set = this.allListeners.get(type);
    if (set && set.size > 0) {
      set.forEach((fn) => {
        fn(event);
      });
    } else {
      this.listeners.get(type)?.(event);
    }
  }
}

export const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);

export function setupSessionStreamMock(createdStreams: FakeStream[]): MockInstance {
  queryClient.clear();
  return vi.spyOn(streamModule, "openStream").mockImplementation((url: string) => {
    const stream = new FakeStream(url);
    createdStreams.push(stream);
    return stream;
  });
}
