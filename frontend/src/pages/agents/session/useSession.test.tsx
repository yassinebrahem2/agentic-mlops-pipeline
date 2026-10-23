import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import { queryClient } from "@/lib/query";
import * as streamModule from "./stream";
import { STREAM_CLOSED, STREAM_CONNECTING, STREAM_OPEN, type StreamLike } from "./stream";
import { useSession } from "./useSession";

const initialSession = {
  id: "s1",
  agent: "orchestrator",
  busy: false,
  messages: [],
  last_seq: 4,
};

class FakeStream implements StreamLike {
  url: string;
  readyState = STREAM_OPEN;
  onerror: ((event: Event) => void) | null = null;
  listeners = new Map<string, (event: Event) => void>();

  constructor(url: string) {
    this.url = url;
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    if (typeof listener === "function") {
      this.listeners.set(type, listener);
    }
  }

  close = vi.fn(() => {
    this.readyState = STREAM_CLOSED;
  });

  emitOpen(): void {
    this.listeners.get("open")?.(new Event("open"));
  }

  emit(type: string, data: unknown, seq: number): void {
    this.listeners.get(type)?.({
      type,
      data: JSON.stringify(data),
      lastEventId: String(seq),
    } as MessageEvent);
  }
}

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);

describe("useSession", () => {
  let createdStreams: FakeStream[] = [];
  let getSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

  beforeEach(() => {
    createdStreams = [];
    queryClient.clear();
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockImplementation((url: string) => {
      const stream = new FakeStream(url);
      createdStreams.push(stream);
      return stream;
    });
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
