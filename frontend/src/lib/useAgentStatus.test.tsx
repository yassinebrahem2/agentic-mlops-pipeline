import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { queryClient } from "@/lib/query";
import { FakeStream, wrapper } from "@/pages/agents/session/testing";
import * as streamModule from "@/pages/agents/session/stream";
import { useAgentStatus, type AgentStatus } from "./useAgentStatus";

describe("useAgentStatus", () => {
  let createdStreams: FakeStream[] = [];
  let openStreamSpy: MockInstance | undefined;
  let getSpy: MockInstance | undefined;

  beforeEach(() => {
    queryClient.clear();
    createdStreams = [];
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockImplementation((url: string) => {
      const stream = new FakeStream(url);
      createdStreams.push(stream);
      return stream;
    });
  });

  afterEach(() => {
    openStreamSpy?.mockRestore();
    getSpy?.mockRestore();
    vi.useRealTimers();
  });

  it("fetches agent status and refetches on project events debounced", async () => {
    vi.useFakeTimers();

    let callCount = 0;
    const initialStatuses: AgentStatus[] = [
      {
        agent: "orchestrator",
        display_name: "Orchestrator",
        state: "idle",
        task: null,
        open_prs: [],
        last_finished: null,
      },
    ];
    const updatedStatuses: AgentStatus[] = [
      {
        agent: "orchestrator",
        display_name: "Orchestrator",
        state: "running",
        task: null,
        open_prs: [],
        last_finished: null,
      },
    ];

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/agents/status") {
        callCount += 1;
        return Promise.resolve({
          data: callCount === 1 ? initialStatuses : updatedStatuses,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
    });

    const { result } = renderHook(() => useAgentStatus("prj_1"), { wrapper });

    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(result.current.data).toEqual(initialStatuses);
    expect(callCount).toBe(1);

    expect(createdStreams).toHaveLength(1);
    act(() => {
      createdStreams[0]?.emitOpen();
      createdStreams[0]?.emit("turn.started", { type: "turn.started" }, 1);
    });

    // Before debounce timer expires, no extra GET call
    expect(callCount).toBe(1);

    // Advance debounce timer (150ms)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });

    expect(callCount).toBe(2);
    expect(result.current.data).toEqual(updatedStatuses);
  });
});
