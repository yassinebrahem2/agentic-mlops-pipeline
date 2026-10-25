import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import * as streamModule from "@/lib/stream";
import { FakeStream } from "@/pages/agents/session/testing";
import { useProjectEvents, type EventRecord } from "./useProjectEvents";

describe("useProjectEvents", () => {
  let createdStreams: FakeStream[] = [];
  let openStreamSpy: MockInstance | undefined;

  beforeEach(() => {
    createdStreams = [];
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockImplementation((url: string) => {
      const stream = new FakeStream(url);
      createdStreams.push(stream);
      return stream;
    });
  });

  afterEach(() => {
    openStreamSpy?.mockRestore();
    vi.useRealTimers();
  });

  it("connects to /api/projects/{projectId}/events/stream and updates status on open", () => {
    const { result } = renderHook(() => useProjectEvents("prj_1"));

    expect(createdStreams).toHaveLength(1);
    expect(createdStreams[0]?.url).toBe("/api/projects/prj_1/events/stream");
    expect(result.current.connection).toBe("closed");

    act(() => {
      createdStreams[0]?.emitOpen();
    });

    expect(result.current.connection).toBe("live");
  });

  it("dispatches parsed project events to onEvent callback", () => {
    const onEvent = vi.fn();
    renderHook(() => useProjectEvents("prj_1", { onEvent }));

    const mockRecord: EventRecord = {
      id: "ev_1",
      seq: 1,
      ts: "2026-10-25T00:00:00Z",
      source: "orchestrator",
      type: "turn.started",
      subject_id: "trn_1",
      payload: {},
    };

    act(() => {
      createdStreams[0]?.emitOpen();
      createdStreams[0]?.emit("turn.started", mockRecord, 1);
    });

    expect(onEvent).toHaveBeenCalledWith(mockRecord);
  });

  it("dispatches resync to onResync callback", () => {
    const onResync = vi.fn();
    renderHook(() => useProjectEvents("prj_1", { onResync }));

    act(() => {
      createdStreams[0]?.emitOpen();
      createdStreams[0]?.emit("resync", {}, 0);
    });

    expect(onResync).toHaveBeenCalled();
  });

  it("reconnects on invalid JSON rather than swallowing", () => {
    vi.useFakeTimers();
    const onEvent = vi.fn();
    const { result } = renderHook(() => useProjectEvents("prj_1", { onEvent }));

    act(() => {
      createdStreams[0]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");

    act(() => {
      createdStreams[0]?.emit("turn.started", "{not json", 1);
    });

    expect(result.current.connection).toBe("reconnecting");
    expect(createdStreams[0]?.close).toHaveBeenCalled();
  });

  it("schedules reconnect on error", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useProjectEvents("prj_1"));

    act(() => {
      createdStreams[0]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");

    act(() => {
      createdStreams[0]?.onerror?.(new Event("error"));
    });
    expect(result.current.connection).toBe("reconnecting");

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(createdStreams).toHaveLength(2);
  });

  it("cleans up stream on unmount", () => {
    const { unmount } = renderHook(() => useProjectEvents("prj_1"));

    expect(createdStreams).toHaveLength(1);
    unmount();

    expect(createdStreams[0]?.close).toHaveBeenCalled();
  });
});
