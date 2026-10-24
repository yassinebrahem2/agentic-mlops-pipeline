import { useParams } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { components } from "@/api/schema";
import type { PageRoute } from "@/app/routes";
import { resetCurrentProjectStore } from "@/lib/useCurrentProject";
import { renderPage } from "@/test/render";
import { route as projectHomeRoute } from "../../pages/project-home/route";
import { route as projectSettingsRoute } from "../../pages/project-settings/route";

type Project = components["schemas"]["Project"];

const fakeProjects: Project[] = [
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

function ProjectSettingsDisplay() {
  const { projectId } = useParams<{ projectId: string }>();
  return <div>Project Settings Page for {projectId}</div>;
}

const fakeProjectSettingsRoute: PageRoute = {
  path: "/projects/:projectId/settings",
  element: <ProjectSettingsDisplay />,
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

const testRoutes: PageRoute[] = [
  fakeRootRoute,
  fakeProjectsNewRoute,
  projectHomeRoute,
  fakeProjectSettingsRoute,
];

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
    resetCurrentProjectStore();
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

      if (method === "GET") {
        const app: components["schemas"]["AppInfo"] = {
          name: "App",
          version: "1",
          mode: "local",
          data_dir: "/data",
        };
        const agents: components["schemas"]["AgentInfo"][] = [];
        const models: components["schemas"]["ModelEntry"][] = [];
        const providers: components["schemas"]["Provider"][] = [];
        const data: unknown = url.endsWith("/api/app")
          ? app
          : url.endsWith("/agents")
            ? agents
            : url.endsWith("/api/models")
              ? models
              : url.endsWith("/api/providers")
                ? providers
                : fakeProjects.find((project) => url.endsWith(`/api/projects/${project.id}`));
        if (data !== undefined)
          return Promise.resolve(
            new Response(JSON.stringify(data), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
      }

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

  it("never treats 'new' as a project at /projects/new", async () => {
    setupFetch();

    renderPage("/projects/new", { routes: testRoutes });

    // No project in the URL: the latest opened one is current, and "new" is not a project.
    expect(await screen.findByRole("button", { name: "Alpha Project" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "new" })).not.toBeInTheDocument();
  });

  it("shows 'Projects' at root /", async () => {
    setupFetch();

    renderPage("/", { routes: testRoutes });

    expect(await screen.findByRole("button", { name: "Projects" })).toBeInTheDocument();
  });

  it("shows a failed project list lookup in the switcher on a project page", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(null, { status: 503 }))),
    );
    renderPage("/projects/prj_alpha/settings", { routes: testRoutes });
    await user.click(await screen.findByRole("button", { name: "Projects" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server answered with an error (HTTP 503).",
    );
  });

  it("selecting another project opens it via POST /open and navigates to the project", async () => {
    const user = userEvent.setup();
    let openedId = "";
    setupFetch((id) => {
      openedId = id;
    });

    renderPage("/projects/prj_alpha/settings", {
      routes: testRoutes.map((route) =>
        route === fakeProjectSettingsRoute ? projectSettingsRoute : route,
      ),
    });

    const switcherButton = await screen.findByRole("button", { name: "Alpha Project" });
    await user.click(switcherButton);

    const betaItem = screen.getByRole("menuitem", { name: "Beta Project" });
    await user.click(betaItem);

    expect(openedId).toBe("prj_beta");
    expect(await screen.findByRole("button", { name: "Beta Project" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Project settings" })).toBeInTheDocument();
    expect(await screen.findByText("org/beta")).toBeInTheDocument();
  });

  it("shows open error alert in the dropdown when opening a project fails", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = getUrlAndMethod(input, init);
      if (url.includes("/api/projects/prj_beta/open") && method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({ code: "folder_not_found", message: "Folder does not exist" }),
            { status: 404, headers: { "Content-Type": "application/json" } },
          ),
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

    renderPage("/projects/prj_alpha/settings", { routes: testRoutes });

    const switcherButton = await screen.findByRole("button", { name: "Alpha Project" });
    await user.click(switcherButton);

    const betaItem = screen.getByRole("menuitem", { name: "Beta Project" });
    await user.click(betaItem);

    expect(await screen.findByRole("alert")).toHaveTextContent("Folder does not exist");
  });
});
