import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createQueryWrapper } from "../test/queryWrapper";
import { projectAgentsKey, useProjectAgents } from "./useProjectAgents";

describe("useProjectAgents", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("builds the project agents query key", () => {
    expect(projectAgentsKey("prj_123")).toEqual(["projects", "prj_123", "agents"]);
  });

  it("fetches project agents using queryKey and path /api/projects/{project_id}/agents", async () => {
    const fakeAgents = [
      {
        name: "agent-1",
        display_name: "Agent One",
        chat: true,
        settings: {
          project_model: null,
          default_model: null,
          effective_model: "provider/model",
          model_problem: null,
          max_steps: 10,
        },
      },
    ];
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes("/api/projects/prj_123/agents")) {
        return Promise.resolve(
          new Response(JSON.stringify(fakeAgents), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProjectAgents("prj_123"), {
      wrapper: createQueryWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toEqual(fakeAgents);
  });

  it("surfaces error when GET /api/projects/{project_id}/agents fails", async () => {
    const fakeFetch = vi.fn().mockImplementation(() => {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            code: "internal_error",
            message: "Failed to load agents",
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProjectAgents("prj_123"), {
      wrapper: createQueryWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it("is disabled when projectId is undefined", () => {
    const fakeFetch = vi.fn();
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProjectAgents(undefined), {
      wrapper: createQueryWrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(fakeFetch).not.toHaveBeenCalled();
  });
});
