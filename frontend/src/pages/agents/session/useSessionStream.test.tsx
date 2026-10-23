import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import {
  FakeStream,
  initialSession,
  setupSessionStreamMock,
  STREAM_CLOSED,
  wrapper,
} from "./testing";
import { useSession } from "./useSession";

describe("useSessionStream", () => {
  let createdStreams: FakeStream[] = [];
  let getSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

  beforeEach(() => {
    createdStreams = [];
    openStreamSpy = setupSessionStreamMock(createdStreams);
  });

  afterEach(() => {
    getSpy?.mockRestore();
    openStreamSpy?.mockRestore();
    vi.useRealTimers();
  });

  it("handles the resync flow: closes, reloads, replaces state, and reopens with new seq", async () => {
    let callCount = 0;
    getSpy = vi.spyOn(api, "GET").mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          data: initialSession,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.resolve({
        data: {
          ...initialSession,
          last_seq: 10,
          messages: [
            {
              id: "m_synced",
              turn_id: "t1",
              author_kind: "agent" as const,
              author_name: "orchestrator",
              from_agent: null,
              parts: [{ type: "text" as const, text: "reloaded" }],
              status: "complete" as const,
              created_at: "2026-01-01T00:00:00Z",
            },
          ],
        },
        response: new Response(null, { status: 200 }),
      });
    });

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    act(() => {
      createdStreams[0]?.emitOpen();
    });

    act(() => {
      createdStreams[0]?.emit("resync", {}, 5);
    });

    expect(createdStreams[0]?.close).toHaveBeenCalled();

    await waitFor(() => {
      expect(createdStreams).toHaveLength(2);
    });

    expect(createdStreams[1]?.url).toBe(
      "/api/projects/prj_1/agents/orchestrator/session/stream?after=10",
    );
    expect(result.current.state?.messages).toHaveLength(1);
    expect(result.current.state?.lastSeq).toBe(10);

    unmount();
  });

  it("reconnects with exponential backoff on dropped connection, delay grows and resets on open", async () => {
    vi.useFakeTimers();

    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    const { unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(createdStreams).toHaveLength(1);

    // First drop: delay is 1000ms
    await act(async () => {
      const stream = createdStreams[0];
      if (stream) {
        stream.readyState = STREAM_CLOSED;
        stream.onerror?.(new Event("error"));
      }
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(createdStreams).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(createdStreams).toHaveLength(2);

    // Second drop immediately: delay grows to 2000ms
    await act(async () => {
      const stream = createdStreams[1];
      if (stream) {
        stream.readyState = STREAM_CLOSED;
        stream.onerror?.(new Event("error"));
      }
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(createdStreams).toHaveLength(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(createdStreams).toHaveLength(3);

    // Third stream opens: resets retry count
    act(() => {
      createdStreams[2]?.emitOpen();
    });

    // Third drop after open: delay resets to 1000ms (not 5000ms)
    await act(async () => {
      const stream = createdStreams[2];
      if (stream) {
        stream.readyState = STREAM_CLOSED;
        stream.onerror?.(new Event("error"));
      }
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(createdStreams).toHaveLength(3);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(createdStreams).toHaveLength(4);

    unmount();
  });

  it("malformed SSE event triggers stream error and reconnection", async () => {
    vi.useFakeTimers();

    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(createdStreams).toHaveLength(1);

    act(() => {
      createdStreams[0]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");

    act(() => {
      createdStreams[0]?.listeners.get("text.delta")?.({
        type: "text.delta",
        data: "{malformed json",
        lastEventId: "5",
      } as MessageEvent);
    });

    expect(createdStreams[0]?.close).toHaveBeenCalled();
    expect(result.current.connection).toBe("reconnecting");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(createdStreams).toHaveLength(2);

    unmount();
  });
});
