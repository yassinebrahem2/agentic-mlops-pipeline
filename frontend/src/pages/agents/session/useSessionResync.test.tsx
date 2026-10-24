import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import { FakeStream, initialSession, setupSessionStreamMock, wrapper } from "./testing";
import { useSession } from "./useSession";

describe("useSessionResync", () => {
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

  it("retries snapshot reload on 500 during resync and succeeds on next scheduled retry", async () => {
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
      // Call 2 in resync handler fails with 500
      if (callCount === 2) {
        return Promise.reject(new ApiError("server_error", "Server error during resync", 500));
      }
      // Call 3 in scheduled connect(true) succeeds
      return Promise.resolve({
        data: { ...initialSession, last_seq: 18 },
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

    // Emit resync: closes stream 1, triggers Call 2 (500) -> schedules retry at 1000ms
    act(() => {
      createdStreams[0]?.emit("resync", {}, 5);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(createdStreams[0]?.close).toHaveBeenCalled();
    expect(result.current.connection).toBe("reconnecting");
    expect(createdStreams).toHaveLength(1);

    // Advance 1000ms: connect(true) fires -> Call 3 succeeds -> opens stream 2
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(createdStreams).toHaveLength(2);
    expect(createdStreams[1]?.url).toContain("?after=18");

    act(() => {
      createdStreams[1]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");
    expect(result.current.state?.lastSeq).toBe(18);

    unmount();
  });

  it("retries snapshot reload on network error during resync and succeeds on next scheduled retry", async () => {
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
      // Call 2 in resync handler fails with network error
      if (callCount === 2) {
        return Promise.reject(new TypeError("Network error during resync"));
      }
      // Call 3 in scheduled connect(true) succeeds
      return Promise.resolve({
        data: { ...initialSession, last_seq: 25 },
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
      createdStreams[0]?.emit("resync", {}, 5);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(createdStreams[0]?.close).toHaveBeenCalled();
    expect(result.current.connection).toBe("reconnecting");
    expect(createdStreams).toHaveLength(1);

    // Advance 1000ms: connect(true) fires -> Call 3 succeeds -> opens stream 2
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(createdStreams).toHaveLength(2);
    expect(createdStreams[1]?.url).toContain("?after=25");

    act(() => {
      createdStreams[1]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");
    expect(result.current.state?.lastSeq).toBe(25);

    unmount();
  });

  it("stream stays closed when resync reload fails with 404", async () => {
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
      createdStreams[0]?.emit("resync", {}, 5);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(result.current.connection).toBe("closed");
    expect(result.current.error?.status).toBe(404);

    // Advance 10s: no retry scheduled, stays closed
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });

    expect(createdStreams).toHaveLength(1);
    expect(result.current.connection).toBe("closed");

    unmount();
  });
});
