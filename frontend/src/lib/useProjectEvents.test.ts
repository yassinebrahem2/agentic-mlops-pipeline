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

  describe("shared stream", () => {
    const record = (seq: number): EventRecord => ({
      id: `ev_${String(seq)}`,
      seq,
      ts: "2026-10-25T00:00:00Z",
      source: "orchestrator",
      type: "turn.started",
      subject_id: "trn_1",
      payload: {},
    });

    it("opens one stream for several subscribers and delivers every event and resync to each", () => {
      const first = { onEvent: vi.fn(), onResync: vi.fn() };
      const second = { onEvent: vi.fn(), onResync: vi.fn() };
      renderHook(() => useProjectEvents("prj_1", first));
      renderHook(() => useProjectEvents("prj_1", second));

      expect(createdStreams).toHaveLength(1);

      act(() => {
        createdStreams[0]?.emitOpen();
        createdStreams[0]?.emit("turn.started", record(1), 1);
        createdStreams[0]?.emit("turn.started", record(2), 2);
        createdStreams[0]?.emit("resync", {}, 0);
      });

      for (const subscriber of [first, second]) {
        expect(subscriber.onEvent.mock.calls).toEqual([[record(1)], [record(2)]]);
        expect(subscriber.onResync).toHaveBeenCalledTimes(1);
      }
    });

    it("closes the stream only when the last subscriber unmounts, and reopens after", () => {
      const first = renderHook(() => useProjectEvents("prj_1"));
      const second = renderHook(() => useProjectEvents("prj_1"));

      first.unmount();
      expect(createdStreams[0]?.close).not.toHaveBeenCalled();

      second.unmount();
      expect(createdStreams[0]?.close).toHaveBeenCalled();

      renderHook(() => useProjectEvents("prj_1"));
      expect(createdStreams).toHaveLength(2);
    });

    it("opens a separate stream per project", () => {
      renderHook(() => useProjectEvents("prj_1"));
      renderHook(() => useProjectEvents("prj_2"));

      expect(createdStreams.map((stream) => stream.url)).toEqual([
        "/api/projects/prj_1/events/stream",
        "/api/projects/prj_2/events/stream",
      ]);
    });

    it("lets a later subscriber join the open stream with its current connection state", () => {
      renderHook(() => useProjectEvents("prj_1"));
      act(() => {
        createdStreams[0]?.emitOpen();
      });

      const onEvent = vi.fn();
      const late = renderHook(() => useProjectEvents("prj_1", { onEvent }));

      expect(createdStreams).toHaveLength(1);
      expect(late.result.current.connection).toBe("live");

      act(() => {
        createdStreams[0]?.emit("turn.started", record(1), 1);
      });
      expect(onEvent).toHaveBeenCalledWith(record(1));
    });

    it("keeps delivering to the others when one subscriber unmounts", () => {
      const leaving = { onEvent: vi.fn() };
      const staying = { onEvent: vi.fn() };
      const left = renderHook(() => useProjectEvents("prj_1", leaving));
      renderHook(() => useProjectEvents("prj_1", staying));

      left.unmount();
      act(() => {
        createdStreams[0]?.emitOpen();
        createdStreams[0]?.emit("turn.started", record(1), 1);
      });

      expect(leaving.onEvent).not.toHaveBeenCalled();
      expect(staying.onEvent).toHaveBeenCalledWith(record(1));
    });

    it("logs a throwing callback and still delivers to the others without reconnecting", () => {
      const failure = new Error("subscriber failed");
      const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
      const throwing = renderHook(() =>
        useProjectEvents("prj_1", {
          onEvent: () => {
            throw failure;
          },
        }),
      );
      const onEvent = vi.fn();
      renderHook(() => useProjectEvents("prj_1", { onEvent }));

      act(() => {
        createdStreams[0]?.emitOpen();
        createdStreams[0]?.emit("turn.started", record(1), 1);
      });

      expect(onEvent).toHaveBeenCalledWith(record(1));
      expect(consoleError).toHaveBeenCalledWith(expect.any(String), failure);
      expect(createdStreams[0]?.close).not.toHaveBeenCalled();
      expect(createdStreams).toHaveLength(1);
      expect(throwing.result.current.connection).toBe("live");
      consoleError.mockRestore();
    });

    it("reconnects once for all subscribers, resuming after the last seq until a resync", () => {
      vi.useFakeTimers();
      const first = renderHook(() => useProjectEvents("prj_1"));
      const second = renderHook(() => useProjectEvents("prj_1"));

      act(() => {
        createdStreams[0]?.emitOpen();
        createdStreams[0]?.emit("turn.started", record(7), 7);
        createdStreams[0]?.onerror?.(new Event("error"));
      });
      expect(first.result.current.connection).toBe("reconnecting");
      expect(second.result.current.connection).toBe("reconnecting");

      act(() => {
        vi.advanceTimersByTime(999);
      });
      expect(createdStreams).toHaveLength(1);
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(createdStreams.map((stream) => stream.url)).toEqual([
        "/api/projects/prj_1/events/stream",
        "/api/projects/prj_1/events/stream?after=7",
      ]);

      act(() => {
        createdStreams[1]?.emitOpen();
      });
      expect(first.result.current.connection).toBe("live");
      expect(second.result.current.connection).toBe("live");

      act(() => {
        createdStreams[1]?.emit("resync", {}, 0);
        createdStreams[1]?.onerror?.(new Event("error"));
        vi.advanceTimersByTime(1000);
      });
      expect(createdStreams).toHaveLength(3);
      expect(createdStreams[2]?.url).toBe("/api/projects/prj_1/events/stream");
    });

    it("calls the latest callbacks without reopening the stream", () => {
      const firstCallback = vi.fn();
      const latestCallback = vi.fn();
      const { rerender } = renderHook(
        ({ callback }: { callback: (event: EventRecord) => void }) =>
          useProjectEvents("prj_1", {
            onEvent: (event) => {
              callback(event);
            },
          }),
        { initialProps: { callback: firstCallback } },
      );

      rerender({ callback: latestCallback });
      act(() => {
        createdStreams[0]?.emitOpen();
        createdStreams[0]?.emit("turn.started", record(1), 1);
      });

      expect(createdStreams).toHaveLength(1);
      expect(firstCallback).not.toHaveBeenCalled();
      expect(latestCallback).toHaveBeenCalledWith(record(1));
    });

    it("closes the old project's stream when its last subscriber switches project", () => {
      const { rerender } = renderHook(
        ({ projectId }: { projectId: string }) => useProjectEvents(projectId),
        { initialProps: { projectId: "prj_1" } },
      );

      rerender({ projectId: "prj_2" });

      expect(createdStreams[0]?.close).toHaveBeenCalled();
      expect(createdStreams[1]?.url).toBe("/api/projects/prj_2/events/stream");
    });

    it("opens no stream without a project and moves to the new project's stream on switch", () => {
      const other = renderHook(() => useProjectEvents("prj_1"));
      const initialProps: { projectId: string | undefined } = { projectId: undefined };
      const { result, rerender } = renderHook(
        ({ projectId }: { projectId: string | undefined }) => useProjectEvents(projectId),
        { initialProps },
      );
      expect(createdStreams).toHaveLength(1);
      expect(result.current.connection).toBe("closed");

      rerender({ projectId: "prj_1" });
      expect(createdStreams).toHaveLength(1);
      act(() => {
        createdStreams[0]?.emitOpen();
      });
      expect(result.current.connection).toBe("live");

      rerender({ projectId: "prj_2" });
      expect(createdStreams[1]?.url).toBe("/api/projects/prj_2/events/stream");
      expect(result.current.connection).toBe("closed");
      expect(createdStreams[0]?.close).not.toHaveBeenCalled();

      other.unmount();
      expect(createdStreams[0]?.close).toHaveBeenCalled();

      rerender({ projectId: undefined });
      expect(createdStreams[1]?.close).toHaveBeenCalled();
      expect(result.current.connection).toBe("closed");
    });
  });
});
