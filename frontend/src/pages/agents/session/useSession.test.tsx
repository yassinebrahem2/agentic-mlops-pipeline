import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import { queryClient } from "@/lib/query";
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

describe("useSession", () => {
  let createdStreams: FakeStream[] = [];
  let getSpy: MockInstance | undefined;

  beforeEach(() => {
    createdStreams = [];
    queryClient.clear();
  });

  afterEach(() => {
    getSpy?.mockRestore();
    vi.useRealTimers();
  });

  const makeStream = (url: string): FakeStream => {
    const stream = new FakeStream(url);
    createdStreams.push(stream);
    return stream;
  };

  it("loads the session, opens stream with ?after=<last_seq>, and folds events", async () => {
    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    const { result, unmount } = renderHook(() =>
      useSession("prj_1", "orchestrator", { openStream: makeStream }),
    );

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });

    expect(createdStreams[0]?.url).toBe(
      "/api/projects/prj_1/agents/orchestrator/session/stream?after=4",
    );
    expect(result.current.connection).toBe("closed");

    // Firing open event transitions connection to live
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

    const { result, unmount } = renderHook(() =>
      useSession("prj_1", "orchestrator", { openStream: makeStream }),
    );

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    act(() => {
      createdStreams[0]?.emitOpen();
    });

    // Emit resync event
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

  it("CONNECTING makes no request and sets connection to reconnecting", async () => {
    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    const { result, unmount } = renderHook(() =>
      useSession("prj_1", "orchestrator", { openStream: makeStream }),
    );

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    act(() => {
      createdStreams[0]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");

    // Stream signals reconnecting on its own
    act(() => {
      const stream = createdStreams[0];
      if (stream) {
        stream.readyState = STREAM_CONNECTING;
        stream.onerror?.(new Event("error"));
      }
    });

    expect(result.current.connection).toBe("reconnecting");
    // Only the initial load call was made, no extra GET request
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

    const { result, unmount } = renderHook(() =>
      useSession("prj_1", "orchestrator", { openStream: makeStream }),
    );

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

  it("reconnects with exponential backoff on dropped connection", async () => {
    vi.useFakeTimers();

    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    const { unmount } = renderHook(() =>
      useSession("prj_1", "orchestrator", { openStream: makeStream }),
    );

    // Initial load resolves
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(createdStreams).toHaveLength(1);

    // Stream drops
    await act(async () => {
      const stream = createdStreams[0];
      if (stream) {
        stream.readyState = STREAM_CLOSED;
        stream.onerror?.(new Event("error"));
      }
      await vi.advanceTimersByTimeAsync(0);
    });

    // Before backoff delay (1000ms), no new stream yet
    expect(createdStreams).toHaveLength(1);

    // Advance 1000ms
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(createdStreams).toHaveLength(2);

    unmount();
  });

  it("closes the stream when the agent changes", async () => {
    getSpy = vi.spyOn(api, "GET").mockResolvedValue({
      data: initialSession,
      response: new Response(null, { status: 200 }),
    });

    let currentAgent = "orchestrator";
    const { rerender, unmount } = renderHook(() =>
      useSession("prj_1", currentAgent, { openStream: makeStream }),
    );

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    const firstStream = createdStreams[0];

    // Change agent
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

    const { result, unmount } = renderHook(() =>
      useSession("prj_1", "orchestrator", { openStream: makeStream }),
    );

    await waitFor(() => {
      expect(result.current.connection).toBe("closed");
      expect(result.current.error).toBeDefined();
      expect(result.current.error?.code).toBe("load_failed");
    });

    // Calling reload retries
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

  it("failed resync reload leaves connection closed, not live", async () => {
    let callCount = 0;
    getSpy = vi.spyOn(api, "GET").mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          data: initialSession,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new TypeError("Network drop during resync"));
    });

    const { result, unmount } = renderHook(() =>
      useSession("prj_1", "orchestrator", { openStream: makeStream }),
    );

    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    act(() => {
      createdStreams[0]?.emitOpen();
    });
    expect(result.current.connection).toBe("live");

    act(() => {
      createdStreams[0]?.emit("resync", {}, 5);
    });

    await waitFor(() => {
      expect(result.current.connection).toBe("closed");
      expect(result.current.error?.code).toBe("load_failed");
    });

    unmount();
  });
});
