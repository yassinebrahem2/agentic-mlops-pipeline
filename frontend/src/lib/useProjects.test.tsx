import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createQueryWrapper } from "../test/queryWrapper";
import { projectsKey, useProjects } from "./useProjects";

describe("useProjects", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("builds the projects query key", () => {
    expect(projectsKey).toEqual(["projects"]);
  });

  it("fetches GET /api/projects", async () => {
    const fakeFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([{ id: "prj_1", name: "One" }]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProjects(), { wrapper: createQueryWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual([{ id: "prj_1", name: "One" }]);
    const request = fakeFetch.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe("/api/projects");
  });
});
