import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import * as React from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useOpenProject } from "./useOpenProject";

function getUrlAndMethod(
  input: RequestInfo | URL,
  init?: RequestInit,
): { url: string; method: string } {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const method = init?.method ?? (input instanceof Request ? input.method : "GET");
  return { url, method };
}

function createWrapper(initialEntries = ["/"]) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={initialEntries}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  };
}

describe("useOpenProject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("handles successful open and calls onNavigate", async () => {
    const onNavigate = vi.fn();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = getUrlAndMethod(input, init);
      if (url.includes("/api/projects/prj_1/open") && method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              id: "prj_1",
              name: "Prj 1",
              source: "local",
              status: "ok",
              status_detail: null,
              created_at: "2026-10-01T00:00:00Z",
              last_opened_at: "2026-10-21T00:00:00Z",
              local_path: "/workspace/prj1",
              github_full_name: null,
              default_branch: null,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useOpenProject({ onNavigate }), {
      wrapper: createWrapper(),
    });

    act(() => {
      result.current.openProject("prj_1");
    });

    expect(result.current.openingId).toBe("prj_1");

    await waitFor(() => {
      expect(result.current.openingId).toBeNull();
    });

    expect(onNavigate).toHaveBeenCalled();
    expect(result.current.error).toBeNull();
    expect(result.current.errorProjectId).toBeNull();
  });

  it("handles failed open, sets error and errorProjectId without throwing", async () => {
    const onNavigate = vi.fn();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = getUrlAndMethod(input, init);
      if (url.includes("/api/projects/prj_fail/open") && method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "folder_not_found",
              message: "Folder does not exist on disk",
            }),
            { status: 404, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useOpenProject({ onNavigate }), {
      wrapper: createWrapper(),
    });

    act(() => {
      result.current.openProject("prj_fail");
    });

    await waitFor(() => {
      expect(result.current.openingId).toBeNull();
    });

    expect(onNavigate).not.toHaveBeenCalled();
    expect(result.current.error).toBe("Folder does not exist on disk");
    expect(result.current.errorProjectId).toBe("prj_fail");

    act(() => {
      result.current.clearError();
    });
    expect(result.current.error).toBeNull();
    expect(result.current.errorProjectId).toBeNull();
  });
});
