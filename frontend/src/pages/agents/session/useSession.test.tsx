import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import { queryClient } from "@/lib/query";
import { STREAM_CONNECTING } from "./stream";
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

  it("refreshes current_task on turn.ended", async () => {
    const updatedSession = {
      ...initialSession,
      current_task: {
        id: "tsk_test",
        title: "Test Task",
        branch: "task/test",
        status: "active",
        review_kind: "local",
        pr_number: null,
        pr_url: null,
      },
    };
    getSpy = vi
      .spyOn(api, "GET")
      .mockResolvedValueOnce({
        data: initialSession,
        response: new Response(null, { status: 200 }),
      })
      .mockResolvedValueOnce({
        data: updatedSession,
        response: new Response(null, { status: 200 }),
      });

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });

    act(() => {
      createdStreams[0]?.emitOpen();
    });

    expect(result.current.state?.current_task).toBeUndefined();

    act(() => {
      createdStreams[0]?.emit("turn.ended", { turn_id: "trn_1" }, 5);
    });

    await waitFor(() => {
      expect(result.current.state?.current_task?.id).toBe("tsk_test");
    });

    unmount();
  });

  it("drops stale session snapshot responses arriving out of order", async () => {
    const task1 = {
      id: "tsk_1",
      title: "Task 1",
      branch: "task/1",
      status: "active",
      review_kind: "local",
      pr_number: null,
      pr_url: null,
    };
    const task2 = {
      id: "tsk_2",
      title: "Task 2",
      branch: "task/2",
      status: "active",
      review_kind: "local",
      pr_number: null,
      pr_url: null,
    };

    type SessionPayload = typeof initialSession & { current_task?: typeof task1 | null };
    type Resolver = (val: { data: SessionPayload; response: Response }) => void;
    let resolveFirstRefresh!: Resolver;
    let resolveSecondRefresh!: Resolver;

    getSpy = vi
      .spyOn(api, "GET")
      .mockResolvedValueOnce({
        data: initialSession,
        response: new Response(null, { status: 200 }),
      })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirstRefresh = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecondRefresh = resolve;
          }),
      );

    const { result, unmount } = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    act(() => {
      createdStreams[0]?.emitOpen();
    });

    // Trigger first refresh
    act(() => {
      createdStreams[0]?.emit("turn.ended", { turn_id: "trn_1" }, 5);
    });

    // Trigger second refresh
    act(() => {
      createdStreams[0]?.emit("turn.ended", { turn_id: "trn_2" }, 6);
    });

    // Second refresh resolves FIRST with task2
    act(() => {
      resolveSecondRefresh({
        data: { ...initialSession, current_task: task2 },
        response: new Response(null, { status: 200 }),
      });
    });

    await waitFor(() => {
      expect(result.current.state?.current_task?.id).toBe("tsk_2");
    });

    // First refresh resolves LATER with older task1
    act(() => {
      resolveFirstRefresh({
        data: { ...initialSession, current_task: task1 },
        response: new Response(null, { status: 200 }),
      });
    });

    // Older response must be dropped: current_task remains task2
    expect(result.current.state?.current_task?.id).toBe("tsk_2");

    unmount();
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
