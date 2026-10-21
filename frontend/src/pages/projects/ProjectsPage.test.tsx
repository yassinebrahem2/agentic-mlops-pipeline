import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PageRoute } from "@/app/routes";
import { renderPage } from "@/test/render";
import { route as projectsRoute } from "./route";
import { setProjectHomeRoutesForTesting } from "../project-home/firstProjectPath";
import { route as projectHomeRoute } from "../project-home/route";

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

const fakeSettingsRoute: PageRoute = {
  path: "/projects/:projectId/settings",
  element: <div>Project Settings Page Content</div>,
  nav: { area: "project", label: "Project settings", order: 100 },
};

const testRoutes: PageRoute[] = [projectsRoute, projectHomeRoute, fakeSettingsRoute];

function getUrlAndMethod(
  input: RequestInfo | URL,
  init?: RequestInit,
): { url: string; method: string } {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const method = init?.method ?? (input instanceof Request ? input.method : "GET");
  return { url, method };
}

describe("ProjectsPage", () => {
  beforeEach(() => {
    setProjectHomeRoutesForTesting(testRoutes);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    setProjectHomeRoutesForTesting(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders empty state with 'No projects yet.' and an 'Add project' button when there are no projects", async () => {
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = getUrlAndMethod(input, init);
      if (url.includes("/api/projects")) {
        return Promise.resolve(
          new Response(JSON.stringify([]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderPage("/", { routes: testRoutes });

    expect(await screen.findByText("No projects yet.")).toBeInTheDocument();
    const addButtons = screen.getAllByRole("link", { name: "Add project" });
    expect(addButtons.length).toBeGreaterThan(0);
    expect(addButtons[0]).toHaveAttribute("href", "/projects/new");
  });

  it("shows each project's name, source, status, and last opened, sorted by last opened most recent first", async () => {
    const projects: ProjectStub[] = [
      {
        id: "prj_older",
        name: "Older Project",
        source: "local",
        status: "ok",
        status_detail: null,
        created_at: "2026-10-01T10:00:00Z",
        last_opened_at: "2026-10-10T12:00:00Z",
        local_path: "/workspace/older",
        github_full_name: null,
        default_branch: "main",
      },
      {
        id: "prj_recent",
        name: "Recent Project",
        source: "github",
        status: "ok",
        status_detail: "Using cached clone after network failure",
        created_at: "2026-10-01T10:00:00Z",
        last_opened_at: "2026-10-20T15:30:00Z",
        local_path: null,
        github_full_name: "org/recent",
        default_branch: "main",
      },
      {
        id: "prj_never",
        name: "Never Opened Project",
        source: "local",
        status: "empty",
        status_detail: null,
        created_at: "2026-10-01T10:00:00Z",
        last_opened_at: null,
        local_path: "/workspace/empty",
        github_full_name: null,
        default_branch: null,
      },
      {
        id: "prj_missing",
        name: "Missing Project",
        source: "local",
        status: "missing",
        status_detail: "/path/does/not/exist",
        created_at: "2026-10-01T10:00:00Z",
        last_opened_at: "2026-10-05T09:00:00Z",
        local_path: "/path/does/not/exist",
        github_full_name: null,
        default_branch: "main",
      },
      {
        id: "prj_failed",
        name: "Failed Project",
        source: "github",
        status: "clone_failed",
        status_detail: "git clone returned exit code 128",
        created_at: "2026-10-01T10:00:00Z",
        last_opened_at: "2026-10-02T08:00:00Z",
        local_path: null,
        github_full_name: "org/failed",
        default_branch: "main",
      },
    ];

    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = getUrlAndMethod(input, init);
      if (url.includes("/api/projects")) {
        return Promise.resolve(
          new Response(JSON.stringify(projects), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderPage("/", { routes: testRoutes });

    // Wait for project names to appear
    expect(await screen.findByText("Recent Project")).toBeInTheDocument();
    expect(screen.getByText("Older Project")).toBeInTheDocument();
    expect(screen.getByText("Never Opened Project")).toBeInTheDocument();
    expect(screen.getByText("Missing Project")).toBeInTheDocument();
    expect(screen.getByText("Failed Project")).toBeInTheDocument();

    // Verify Sources
    expect(screen.getAllByText("Local folder").length).toBe(3);
    expect(screen.getAllByText("GitHub").length).toBe(2);

    // Verify Statuses
    expect(screen.getByText("Folder not found")).toBeInTheDocument();
    expect(screen.getByText("/path/does/not/exist")).toBeInTheDocument();
    expect(
      screen.getByText("No committed files yet. Commit your files first."),
    ).toBeInTheDocument();
    expect(screen.getByText("Download failed")).toBeInTheDocument();
    expect(screen.getByText("git clone returned exit code 128")).toBeInTheDocument();
    expect(screen.getByText("Using cached clone after network failure")).toBeInTheDocument();

    // Verify sorting order: Recent Project should appear before Older Project, which is before Missing, Failed, and Never Opened
    const rows = screen.getAllByRole("row");
    // Row 0 is header, rows 1..5 are projects
    expect(rows.length).toBe(6);
    const row1 = rows[1];
    const row2 = rows[2];
    const row3 = rows[3];
    const row4 = rows[4];
    const row5 = rows[5];
    expect(row1).toBeDefined();
    expect(row2).toBeDefined();
    expect(row3).toBeDefined();
    expect(row4).toBeDefined();
    expect(row5).toBeDefined();
    if (row1 && row2 && row3 && row4 && row5) {
      expect(within(row1).getByText("Recent Project")).toBeInTheDocument();
      expect(within(row2).getByText("Older Project")).toBeInTheDocument();
      expect(within(row3).getByText("Missing Project")).toBeInTheDocument();
      expect(within(row4).getByText("Failed Project")).toBeInTheDocument();
      expect(within(row5).getByText("Never Opened Project")).toBeInTheDocument();
    }
  });

  it("clicking a project sends POST /api/projects/{id}/open and navigates to the project", async () => {
    const user = userEvent.setup();
    const projects: ProjectStub[] = [
      {
        id: "prj_demo",
        name: "Demo Project",
        source: "local",
        status: "ok",
        status_detail: null,
        created_at: "2026-10-01T10:00:00Z",
        last_opened_at: "2026-10-20T10:00:00Z",
        local_path: "/workspace/demo",
        github_full_name: null,
        default_branch: "main",
      },
    ];

    let openCalled = false;
    let openedId = "";

    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = getUrlAndMethod(input, init);

      if (url.includes("/api/projects/prj_demo/open") && method === "POST") {
        openCalled = true;
        openedId = "prj_demo";
        return Promise.resolve(
          new Response(JSON.stringify(projects[0]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      if (url.includes("/api/projects") && method === "GET") {
        return Promise.resolve(
          new Response(JSON.stringify(projects), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderPage("/", { routes: testRoutes });

    const projectButton = await screen.findByRole("button", { name: "Demo Project" });
    await user.click(projectButton);

    expect(openCalled).toBe(true);
    expect(openedId).toBe("prj_demo");

    // Navigates to /projects/prj_demo which ProjectHome redirects to fakeSettingsRoute
    expect(await screen.findByText("Project Settings Page Content")).toBeInTheDocument();
  });

  it("shows errorText when GET /api/projects fails", async () => {
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = getUrlAndMethod(input, init);
      if (url.includes("/api/projects")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ code: "server_error", message: "Failed to load projects list" }),
            {
              status: 500,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderPage("/", { routes: testRoutes });

    expect(await screen.findByText("Failed to load projects list")).toBeInTheDocument();
  });
});
