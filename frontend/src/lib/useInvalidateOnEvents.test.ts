import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import * as streamModule from "@/lib/stream";
import type { EventRecord } from "@/lib/useProjectEvents";
import { FakeStream } from "@/pages/agents/session/testing";
import { useInvalidateOnEvents } from "./useInvalidateOnEvents";

describe("useInvalidateOnEvents", () => {
  let createdStreams: FakeStream[] = [];
  let openStreamSpy: MockInstance | undefined;
  let invalidateSpy: MockInstance | undefined;
  let testQueryClient: QueryClient;

  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: testQueryClient }, children);

  beforeEach(() => {
    vi.useFakeTimers();
    testQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    createdStreams = [];
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockImplementation((url: string) => {
      const stream = new FakeStream(url);
      createdStreams.push(stream);
      return stream;
    });
    invalidateSpy = vi.spyOn(testQueryClient, "invalidateQueries").mockResolvedValue(undefined);
  });

  afterEach(() => {
    openStreamSpy?.mockRestore();
    invalidateSpy?.mockRestore();
    vi.useRealTimers();
  });

  const makeEvent = (type: EventRecord["type"], seq: number): EventRecord => ({
    id: `ev_${String(seq)}`,
    seq,
    ts: "2026-10-29T12:00:00Z",
    source: "orchestrator",
    type,
    subject_id: "sub_1",
    payload: {},
  });

  it("invalidates once after a burst of three listed events", () => {
    const key = ["projects", "prj_1", "tracking"] as const;
    renderHook(
      () => {
        useInvalidateOnEvents("prj_1", key, ["tracking.synced"]);
      },
      { wrapper },
    );

    expect(createdStreams).toHaveLength(1);
    act(() => {
      createdStreams[0]?.emitOpen();
    });

    // Burst of 3 listed events within 150ms
    act(() => {
      createdStreams[0]?.emit("tracking.synced", makeEvent("tracking.synced", 1), 1);
    });
    act(() => {
      vi.advanceTimersByTime(50);
      createdStreams[0]?.emit("tracking.synced", makeEvent("tracking.synced", 2), 2);
    });
    act(() => {
      vi.advanceTimersByTime(50);
      createdStreams[0]?.emit("tracking.synced", makeEvent("tracking.synced", 3), 3);
    });

    expect(invalidateSpy).not.toHaveBeenCalled();

    // Advance remaining debounce time (150ms from last event)
    act(() => {
      vi.advanceTimersByTime(150);
    });

    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: key });
  });

  it("does nothing on an unlisted event", () => {
    const key = ["projects", "prj_1", "tracking"] as const;
    renderHook(
      () => {
        useInvalidateOnEvents("prj_1", key, ["tracking.synced"]);
      },
      { wrapper },
    );

    act(() => {
      createdStreams[0]?.emitOpen();
      createdStreams[0]?.emit("turn.started", makeEvent("turn.started", 1), 1);
      vi.advanceTimersByTime(300);
    });

    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("invalidates on resync", () => {
    const key = ["projects", "prj_1", "tracking"] as const;
    renderHook(
      () => {
        useInvalidateOnEvents("prj_1", key, ["tracking.synced"]);
      },
      { wrapper },
    );

    act(() => {
      createdStreams[0]?.emitOpen();
      // Server emits resync event
      createdStreams[0]?.emit("resync", {});
      vi.advanceTimersByTime(150);
    });

    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: key });
  });

  it("cancels pending invalidation on unmount", () => {
    const key = ["projects", "prj_1", "tracking"] as const;
    const { unmount } = renderHook(
      () => {
        useInvalidateOnEvents("prj_1", key, ["tracking.synced"]);
      },
      { wrapper },
    );

    act(() => {
      createdStreams[0]?.emitOpen();
      createdStreams[0]?.emit("tracking.synced", makeEvent("tracking.synced", 1), 1);
    });

    // Unmount before 150ms debounce fires
    unmount();

    act(() => {
      vi.advanceTimersByTime(300);
    });

    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});
