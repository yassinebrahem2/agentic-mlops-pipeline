import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import { STREAM_CLOSED } from "./stream";
import { FakeStream, initialSession, setupSessionStreamMock, wrapper } from "./testing";
import { useSession } from "./useSession";

describe("useSessionReconnect", () => {
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

  it("retries snapshot reload on 500 during reconnect and succeeds on next scheduled retry", async () => {
    vi.useFakeTimers();
    let callCount = 0;
    getSpy = vi.spyOn(api, "GET").mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          data: initialSession,
          response: new Response(null, { status: 200 }),
        });
      }
      // Call 2 (in handleStreamError) and Call 3 (in scheduled connect(true)) fail with 500
      if (callCount <= 3) {
        return Promise.reject(new ApiError("server_error", "Internal server error", 500));
      }
      // Call 4 succeeds on the second scheduled retry
      return Promise.resolve({
        data: { ...initialSession, last_seq: 15 },
        response: new Response(null, { status: 200 }),
      });
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

    // Drop connection: triggers handleStreamError -> Call 2 fails -> schedules retry at 1000ms
    act(() => {
      const stream = createdStreams[0];
      if (stream) {
        stream.readyState = STREAM_CLOSED;
        stream.onerror?.(new Event("error"));
      }
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.connection).toBe("reconnecting");
    expect(createdStreams).toHaveLength(1);

    // Advance 1000ms: first connect(true) fires -> Call 3 fails with 500 -> schedules retry at 2000ms
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.connection).toBe("reconnecting");
    expect(createdStreams).toHaveLength(1);

    // Advance 2000ms: second connect(true) fires -> Call 4 succeeds -> opens stream 2
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(createdStreams).toHaveLength(2);
    expect(createdStreams[1]?.url).toContain("?after=15");

    act(() => {
      createdStreams[1]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");
    expect(result.current.state?.lastSeq).toBe(15);

    unmount();
  });

  it("retries snapshot reload on network error during reconnect and succeeds on next scheduled retry", async () => {
    vi.useFakeTimers();
    let callCount = 0;
    getSpy = vi.spyOn(api, "GET").mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          data: initialSession,
          response: new Response(null, { status: 200 }),
        });
      }
      // Call 2 (in handleStreamError) and Call 3 (in scheduled connect(true)) fail with network error
      if (callCount <= 3) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      // Call 4 succeeds on the second scheduled retry
      return Promise.resolve({
        data: { ...initialSession, last_seq: 20 },
        response: new Response(null, { status: 200 }),
      });
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
      const stream = createdStreams[0];
      if (stream) {
        stream.readyState = STREAM_CLOSED;
        stream.onerror?.(new Event("error"));
      }
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.connection).toBe("reconnecting");
    expect(createdStreams).toHaveLength(1);

    // Advance 1000ms: first connect(true) fires -> Call 3 fails -> schedules retry at 2000ms
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.connection).toBe("reconnecting");
    expect(createdStreams).toHaveLength(1);

    // Advance 2000ms: second connect(true) fires -> Call 4 succeeds -> opens stream 2
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(createdStreams).toHaveLength(2);
    expect(createdStreams[1]?.url).toContain("?after=20");

    act(() => {
      createdStreams[1]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");
    expect(result.current.state?.lastSeq).toBe(20);

    unmount();
  });

  it("stream stays closed when reconnect reload fails with 404", async () => {
    vi.useFakeTimers();
    let callCount = 0;
    getSpy = vi.spyOn(api, "GET").mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          data: initialSession,
          response: new Response(null, { status: 200 }),
        });
      }
      // Call 2 in handleStreamError fails with 500 (schedules retry)
      if (callCount === 2) {
        return Promise.reject(new ApiError("server_error", "Server error", 500));
      }
      // Call 3 in connect(true) fails with 404 (refusal)
      return Promise.reject(new ApiError("not_found", "Project prj_1 not found.", 404));
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
      const stream = createdStreams[0];
      if (stream) {
        stream.readyState = STREAM_CLOSED;
        stream.onerror?.(new Event("error"));
      }
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.connection).toBe("reconnecting");

    // Advance 1000ms: connect(true) runs -> Call 3 fails with 404 -> closes permanently
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(result.current.connection).toBe("closed");
    expect(result.current.error?.status).toBe(404);

    // Advance 10s: no new stream created, stays closed
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(createdStreams).toHaveLength(1);
    expect(result.current.connection).toBe("closed");

    unmount();
  });

  it("a refused stream (CLOSED) followed by a 404 closes permanently with error", async () => {
    let callCount = 0;
    getSpy = vi.spyOn(api, "GET").mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          data: initialSession,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new ApiError("not_found", "Project prj_1 not found.", 404));
    });

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await act(async () => {
      await Promise.resolve();
    });
    expect(createdStreams).toHaveLength(1);
    act(() => {
      createdStreams[0]?.emitOpen();
    });

    act(() => {
      const stream = createdStreams[0];
      if (stream) {
        stream.readyState = STREAM_CLOSED;
        stream.onerror?.(new Event("error"));
      }
    });

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.connection).toBe("closed");
    expect(result.current.error?.status).toBe(404);
    expect(result.current.error?.message).toBe("Project prj_1 not found.");
    expect(createdStreams).toHaveLength(1);
    unmount();
  });
});
