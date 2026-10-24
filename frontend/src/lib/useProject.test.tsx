import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createQueryWrapper } from "../test/queryWrapper";
import { projectKey, useProject } from "./useProject";

describe("useProject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("builds the project query key", () => {
    expect(projectKey("prj_123")).toEqual(["projects", "prj_123"]);
  });

  it("fetches project using queryKey and path /api/projects/{project_id}", async () => {
    const fakeProject = {
      id: "prj_123",
      name: "Demo Project",
      source: "local",
      path: "/path/to/project",
      status: "ready",
      last_opened_at: null,
    };
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes("/api/projects/prj_123")) {
        return Promise.resolve(
          new Response(JSON.stringify(fakeProject), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProject("prj_123"), {
      wrapper: createQueryWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toEqual(fakeProject);
  });

  it("surfaces error when GET /api/projects/{project_id} fails", async () => {
    const fakeFetch = vi.fn().mockImplementation(() => {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            code: "not_found",
            message: "Project not found",
          }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProject("prj_missing"), {
      wrapper: createQueryWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it("is disabled when projectId is undefined", () => {
    const fakeFetch = vi.fn();
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProject(undefined), {
      wrapper: createQueryWrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(fakeFetch).not.toHaveBeenCalled();
  });
});
