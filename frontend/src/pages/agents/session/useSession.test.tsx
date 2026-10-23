import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import { queryClient } from "@/lib/query";
import { STREAM_CLOSED, STREAM_CONNECTING } from "./stream";
import { FakeStream, initialSession, setupSessionStreamMock, wrapper } from "./testing";
import { useSession } from "./useSession";

describe("useSession", () => {
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

  it("loads the session, opens stream with ?after=<last_seq>, and folds events", async () => {
    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });

    expect(createdStreams[0]?.url).toBe(
      "/api/projects/prj_1/agents/orchestrator/session/stream?after=4",
    );
    expect(result.current.connection).toBe("closed");

    act(() => {
      createdStreams[0]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");
    expect(result.current.state?.lastSeq).toBe(4);

    act(() => {
      createdStreams[0]?.emit("text.delta", { message_id: "missing", text: "ignored" }, 5);
    });
    expect(result.current.state?.lastSeq).toBe(5);

    unmount();
    expect(createdStreams[0]?.close).toHaveBeenCalled();
  });

  it("CONNECTING makes no request and sets connection to reconnecting", async () => {
    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    act(() => {
      createdStreams[0]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");

    act(() => {
      const stream = createdStreams[0];
      if (stream) {
        stream.readyState = STREAM_CONNECTING;
        stream.onerror?.(new Event("error"));
      }
    });

    expect(result.current.connection).toBe("reconnecting");
    expect(getSpy).toHaveBeenCalledTimes(1);

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

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
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

    await waitFor(() => {
      expect(result.current.connection).toBe("closed");
      expect(result.current.error?.status).toBe(404);
      expect(result.current.error?.message).toBe("Project prj_1 not found.");
    });

    expect(createdStreams).toHaveLength(1);
    unmount();
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
      if (callCount === 2) {
        return Promise.reject(new ApiError("server_error", "Internal server error", 500));
      }
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

    // Advance by backoff delay (1000ms), triggering connect(true) which reloads and succeeds
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
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
      if (callCount === 2) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
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

    // Advance by backoff delay (1000ms), triggering connect(true) which reloads and succeeds
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
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

    expect(result.current.connection).toBe("closed");
    expect(result.current.error?.status).toBe(404);

    // Advance time by 10s: no new stream should be created, stream stays closed
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });

    expect(createdStreams).toHaveLength(1);
    expect(result.current.connection).toBe("closed");

    unmount();
  });

  it("closes the stream when the agent changes", async () => {
    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    let currentAgent = "orchestrator";
    const { rerender, unmount } = renderHook(() => useSession("prj_1", currentAgent), { wrapper });

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    const firstStream = createdStreams[0];

    currentAgent = "reviewer";
    rerender();

    expect(firstStream?.close).toHaveBeenCalled();

    await waitFor(() => {
      expect(createdStreams).toHaveLength(2);
    });
    expect(createdStreams[1]?.url).toContain("/agents/reviewer/session/stream");

    unmount();
  });

  it("failed initial load surfaces error state and allows retry", async () => {
    let shouldFail = true;
    getSpy = vi.spyOn(api, "GET").mockImplementation(() => {
      if (shouldFail) {
        return Promise.reject(new TypeError("Network failed"));
      }
      return Promise.resolve({
        data: initialSession,
        response: new Response(null, { status: 200 }),
      });
    });

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await waitFor(() => {
      expect(result.current.connection).toBe("closed");
      expect(result.current.error).toBeDefined();
      expect(result.current.error?.code).toBe("load_failed");
    });

    shouldFail = false;
    await act(async () => {
      await result.current.reload();
    });

    await waitFor(() => {
      expect(result.current.state?.lastSeq).toBe(4);
      expect(result.current.error).toBeUndefined();
    });

    unmount();
  });

  it("send and stop use mutations and route 401 through queryClient", async () => {
    const postSpy = vi.spyOn(api, "POST").mockResolvedValue({
      data: {},
      response: new Response(null, { status: 200 }),
    });
    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await act(async () => {
      await result.current.send("Hello");
    });
    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/agents/{agent}/session/messages",
      expect.objectContaining({ body: { text: "Hello" } }),
    );

    await act(async () => {
      await result.current.stop();
    });
    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/agents/{agent}/session/stop",
      expect.anything(),
    );

    postSpy.mockRejectedValue(new ApiError("unauthorized", "Unauthorized", 401));
    await expect(result.current.send("Fail")).rejects.toThrow("Unauthorized");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["auth"] });

    postSpy.mockRestore();
    invalidateSpy.mockRestore();
    unmount();
  });
});
