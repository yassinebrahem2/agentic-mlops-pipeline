import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PageRoute } from "@/app/routes";
import { renderPage } from "@/test/render";

interface ProjectStub {
  id: string;
  name: string;
  source: "local" | "github";
  status: "ok" | "missing" | "empty" | "clone_failed";
  status_detail: string | null;
  created_at: string;
  last_opened_at: string | null;
  local_path: string | null;
  github_full_name: string | null;
  default_branch: string | null;
}

const fakeProjects: ProjectStub[] = [
  {
    id: "prj_alpha",
    name: "Alpha Project",
    source: "local",
    status: "ok",
    status_detail: null,
    created_at: "2026-10-01T10:00:00Z",
    last_opened_at: "2026-10-20T10:00:00Z",
    local_path: "/workspace/alpha",
    github_full_name: null,
    default_branch: "main",
  },
  {
    id: "prj_beta",
    name: "Beta Project",
    source: "github",
    status: "ok",
    status_detail: null,
    created_at: "2026-10-02T10:00:00Z",
    last_opened_at: "2026-10-19T10:00:00Z",
    local_path: null,
    github_full_name: "org/beta",
    default_branch: "main",
  },
];

const fakeProjectSettingsRoute: PageRoute = {
  path: "/projects/:projectId/settings",
  element: <div>Project Settings Page for Project</div>,
  nav: { area: "project", label: "Settings", order: 100 },
};

const fakeProjectsNewRoute: PageRoute = {
  path: "/projects/new",
  element: <div>Add New Project Page</div>,
};

const fakeRootRoute: PageRoute = {
  path: "/",
  element: <div>All Projects Home Page</div>,
  nav: { area: "app", label: "Projects", order: 0 },
};

const testRoutes: PageRoute[] = [fakeRootRoute, fakeProjectsNewRoute, fakeProjectSettingsRoute];

function getUrlAndMethod(
  input: RequestInfo | URL,
  init?: RequestInit,
): { url: string; method: string } {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const method = init?.method ?? (input instanceof Request ? input.method : "GET");
  return { url, method };
}

describe("ProjectSwitcher", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function setupFetch(onOpen?: (projectId: string) => void) {
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = getUrlAndMethod(input, init);

      if (url.includes("/api/projects/") && url.endsWith("/open") && method === "POST") {
        const match = url.match(/\/api\/projects\/([^/]+)\/open/);
        const id = match?.[1] ?? "";
        onOpen?.(id);
        const p = fakeProjects.find((proj) => proj.id === id) ?? fakeProjects[0];
        return Promise.resolve(
          new Response(JSON.stringify(p), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      if (url.includes("/api/projects") && method === "GET") {
        return Promise.resolve(
          new Response(JSON.stringify(fakeProjects), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);
  }

  it("shows the current project name when on a project page and lists all projects ending with 'All projects'", async () => {
    const user = userEvent.setup();
    setupFetch();

    renderPage("/projects/prj_alpha/settings", { routes: testRoutes });

    // The switcher menu button should show the active project's name
    const switcherButton = await screen.findByRole("button", { name: "Alpha Project" });
    expect(switcherButton).toBeInTheDocument();

    await user.click(switcherButton);

    // Menu should list other projects and "All projects"
    expect(screen.getByRole("menuitem", { name: "Beta Project" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "All projects" })).toBeInTheDocument();
  });

  it("shows 'Projects' at /projects/new and never treats 'new' as a project", async () => {
    setupFetch();

    renderPage("/projects/new", { routes: testRoutes });

    // Should show "Projects", not "new"
    expect(await screen.findByRole("button", { name: "Projects" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "new" })).not.toBeInTheDocument();
  });

  it("shows 'Projects' at root /", async () => {
    setupFetch();

    renderPage("/", { routes: testRoutes });

    expect(await screen.findByRole("button", { name: "Projects" })).toBeInTheDocument();
  });

  it("selecting another project opens it via POST /open and navigates to the project", async () => {
    const user = userEvent.setup();
    let openedId = "";
    setupFetch((id) => {
      openedId = id;
    });

    renderPage("/projects/prj_alpha/settings", { routes: testRoutes });

    const switcherButton = await screen.findByRole("button", { name: "Alpha Project" });
    await user.click(switcherButton);

    const betaItem = screen.getByRole("menuitem", { name: "Beta Project" });
    await user.click(betaItem);

    expect(openedId).toBe("prj_beta");
  });
});
