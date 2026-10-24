import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { resetCurrentProjectStore, useCurrentProject } from "./useCurrentProject";

type Project = components["schemas"]["Project"];

function project(id: string, lastOpenedAt: string | null): Project {
  return {
    id,
    name: `Name ${id}`,
    source: "local",
    status: "ok",
    status_detail: null,
    created_at: "2026-10-01T10:00:00Z",
    last_opened_at: lastOpenedAt,
    local_path: `/w/${id}`,
    github_full_name: null,
    default_branch: "main",
  };
}

function stubProjects(projects: Project[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify(projects), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
}

let navigateTo: (path: string) => void = () => undefined;

function Navigator() {
  const navigate = useNavigate();
  navigateTo = (path) => {
    void navigate(path);
  };
  return null;
}

function wrapperAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <Navigator />
          <Routes>
            <Route path="/projects/new" element={children} />
            <Route path="/projects/:projectId/*" element={children} />
            <Route path="*" element={children} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

const A = project("prj_a", "2026-10-10T10:00:00Z");
const B = project("prj_b", "2026-10-20T10:00:00Z");

describe("useCurrentProject", () => {
  beforeEach(() => {
    resetCurrentProjectStore();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("is the project in the URL, whatever last_opened_at says", async () => {
    stubProjects([A, B]);
    const { result } = renderHook(() => useCurrentProject(), {
      wrapper: wrapperAt("/projects/prj_a/agents"),
    });
    expect(result.current.projectId).toBe("prj_a");
    await waitFor(() => {
      expect(result.current.project?.id).toBe("prj_a");
    });
  });

  it("keeps the last visited project on an app route", async () => {
    stubProjects([A, B]);
    const { result } = renderHook(() => useCurrentProject(), {
      wrapper: wrapperAt("/projects/prj_a/agents"),
    });
    act(() => {
      navigateTo("/providers");
    });
    await waitFor(() => {
      expect(result.current.project?.id).toBe("prj_a");
    });
  });

  it("falls back to the latest last_opened_at on a fresh load of an app route", async () => {
    stubProjects([A, B]);
    const { result } = renderHook(() => useCurrentProject(), { wrapper: wrapperAt("/providers") });
    await waitFor(() => {
      expect(result.current.project?.id).toBe("prj_b");
    });
    expect(result.current.projectId).toBe("prj_b");
  });

  it("is undefined when no project was ever opened", async () => {
    stubProjects([project("prj_c", null)]);
    const { result } = renderHook(() => useCurrentProject(), { wrapper: wrapperAt("/providers") });
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });
    expect(result.current.projectId).toBeUndefined();
    expect(result.current.project).toBeUndefined();
  });

  it("ignores a remembered project that no longer exists", async () => {
    stubProjects([B]);
    const { result } = renderHook(() => useCurrentProject(), {
      wrapper: wrapperAt("/projects/prj_gone/agents"),
    });
    act(() => {
      navigateTo("/providers");
    });
    await waitFor(() => {
      expect(result.current.projectId).toBe("prj_b");
    });
  });

  it.each(["/", "/projects/new"])("has no project on %s", async (path) => {
    stubProjects([A, B]);
    const { result } = renderHook(() => useCurrentProject(), { wrapper: wrapperAt(path) });
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });
    expect(result.current.projectId).toBeUndefined();
  });
});
