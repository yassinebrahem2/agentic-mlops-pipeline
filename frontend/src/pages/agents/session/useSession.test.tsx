import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../../../lib/api";
import { useSession } from "./useSession";
import { STREAM_CLOSED, STREAM_OPEN, type StreamLike } from "./stream";

const session = {
  id: "s1",
  agent: "orchestrator",
  busy: false,
  messages: [],
  last_seq: 4,
};

class FakeStream implements StreamLike {
  readyState = STREAM_OPEN;
  onerror: ((event: Event) => void) | null = null;
  listeners = new Map<string, (event: Event) => void>();
  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    if (typeof listener === "function") this.listeners.set(type, listener);
  }
  close = vi.fn(() => {
    this.readyState = STREAM_CLOSED;
  });
  emit(type: string, data: unknown, seq: number): void {
    this.listeners.get(type)?.({
      type,
      data: JSON.stringify(data),
      lastEventId: String(seq),
    } as MessageEvent);
  }
}

describe("useSession", () => {
  it("loads the session, opens from its sequence, and folds stream events", async () => {
    const get = vi.spyOn(api, "GET").mockResolvedValue({
      data: session,
      response: new Response(null, { status: 200 }),
    });
    const stream = new FakeStream();
    const result = renderHook(() => {
      return useSession("prj_1", "orchestrator", { openStream: () => stream });
    });
    await waitFor(() => {
      expect(result.result.current.connection).toBe("live");
    });
    expect(get).toHaveBeenCalledWith("/api/projects/prj_1/agents/orchestrator/session");
    act(() => {
      stream.emit("text.delta", { message_id: "missing", text: "ignored" }, 5);
    });
    expect(result.result.current.state?.lastSeq).toBe(5);
    result.unmount();
    expect(stream.close).toHaveBeenCalled();
    get.mockRestore();
  });

  it("closes after the stream is refused for authentication", async () => {
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValue({
      data: session,
      response: new Response(null, { status: 200 }),
    });
    const stream = new FakeStream();
    const result = renderHook(() =>
      useSession("prj_1", "orchestrator", { openStream: () => stream }),
    );
    await waitFor(() => {
      expect(result.result.current.connection).toBe("live");
    });
    get.mockRejectedValue(new ApiError("not_signed_in", "Sign in required", 401));
    act(() => {
      stream.readyState = STREAM_CLOSED;
      stream.onerror?.(new Event("error"));
    });
    await waitFor(() => {
      expect(result.result.current.connection).toBe("closed");
    });
    expect(get).toHaveBeenCalledTimes(2);
    result.unmount();
    get.mockRestore();
  });
});
