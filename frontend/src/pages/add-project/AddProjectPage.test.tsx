import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PageRoute } from "@/app/routes";
import { renderPage } from "@/test/render";
import { AddProjectPage } from "./AddProjectPage";

const testRoutes: PageRoute[] = [
  {
    path: "/projects/new",
    element: <AddProjectPage />,
  },
  {
    path: "/projects/:projectId",
    element: <div>Project Page Content</div>,
  },
  {
    path: "/integrations",
    element: <div>Integrations Page Content</div>,
  },
];

async function getUrlAndMethod(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<{ url: string; method: string; body: unknown }> {
  let url: string;
  let method: string;
  let body: unknown = undefined;

  if (input instanceof Request) {
    url = input.url;
    method = input.method;
    try {
      const cloned = input.clone();
      body = await cloned.json();
    } catch {
      body = undefined;
    }
  } else {
    url = typeof input === "string" ? input : input.href;
    method = init?.method ?? "GET";
    if (init?.body) {
      try {
        body = JSON.parse(init.body as string);
      } catch {
        body = init.body;
      }
    }
  }
  return { url, method, body };
}

describe("AddProjectPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("server mode hides the local tab and shows 'This server works with GitHub repositories.'", async () => {
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const { url } = await getUrlAndMethod(input, init);
        if (url.includes("/api/app")) {
          return new Response(
            JSON.stringify({
              mode: "server",
              name: "mlops_agents",
              version: "0.1.0",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/github")) {
          return new Response(
            JSON.stringify({
              mode: "none",
              gh_available: false,
              login: null,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      });
    vi.stubGlobal("fetch", fakeFetch);

    renderPage("/projects/new", { routes: testRoutes });

    expect(
      await screen.findByText("This server works with GitHub repositories."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Local folder" })).not.toBeInTheDocument();
  });

  it("GitHub not connected shows only the link to Integrations", async () => {
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const { url } = await getUrlAndMethod(input, init);
        if (url.includes("/api/app")) {
          return new Response(
            JSON.stringify({
              mode: "local",
              name: "mlops_agents",
              version: "0.1.0",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/folders")) {
          return new Response(
            JSON.stringify({
              path: "/workspace",
              parent: null,
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/github")) {
          return new Response(
            JSON.stringify({
              mode: "none",
              gh_available: false,
              login: null,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      });
    vi.stubGlobal("fetch", fakeFetch);

    const user = userEvent.setup();
    renderPage("/projects/new", { routes: testRoutes });

    const githubTab = await screen.findByRole("tab", { name: "GitHub" });
    await user.click(githubTab);

    const link = await screen.findByRole("link", { name: "Integrations" });
    expect(link).toHaveAttribute("href", "/integrations");
    expect(link.parentElement).toHaveTextContent("Connect GitHub in Integrations first.");

    // Nothing else on the GitHub tab
    expect(screen.queryByRole("textbox", { name: "Repository" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add" })).not.toBeInTheDocument();
  });

  it("the repository list filters and fills the input", async () => {
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const { url } = await getUrlAndMethod(input, init);
        if (url.includes("/api/app")) {
          return new Response(
            JSON.stringify({
              mode: "local",
              name: "mlops_agents",
              version: "0.1.0",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/folders")) {
          return new Response(
            JSON.stringify({
              path: "/workspace",
              parent: null,
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.endsWith("/api/github")) {
          return new Response(
            JSON.stringify({
              mode: "gh",
              gh_available: true,
              login: "octocat",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/github/repos")) {
          return new Response(
            JSON.stringify([
              {
                full_name: "octocat/hello-world",
                default_branch: "main",
                description: "Sample repo",
                private: false,
              },
              {
                full_name: "octocat/second-project",
                default_branch: "main",
                description: null,
                private: false,
              },
            ]),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      });
    vi.stubGlobal("fetch", fakeFetch);

    const user = userEvent.setup();
    renderPage("/projects/new", { routes: testRoutes });

    await user.click(await screen.findByRole("tab", { name: "GitHub" }));

    expect(await screen.findByText("octocat/hello-world")).toBeInTheDocument();
    expect(screen.getByText("octocat/second-project")).toBeInTheDocument();

    const filterInput = screen.getByRole("textbox", { name: "Filter repositories" });
    await user.type(filterInput, "hello");

    expect(screen.getByText("octocat/hello-world")).toBeInTheDocument();
    expect(screen.queryByText("octocat/second-project")).not.toBeInTheDocument();

    // Click repository to fill the Repository input
    await user.click(screen.getByRole("button", { name: /octocat\/hello-world/i }));

    const repoInput = screen.getByRole("textbox", { name: "Repository" });
    expect(repoInput).toHaveValue("octocat/hello-world");
  });

  it("adding GitHub project sends {source: 'github', repo} and shows progress text until response, then navigates", async () => {
    let resolveAdd: (res: Response) => void = () => {};
    const addPromise = new Promise<Response>((resolve) => {
      resolveAdd = resolve;
    });

    const requests: { url: string; method: string; body: unknown }[] = [];

    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const parsed = await getUrlAndMethod(input, init);
        requests.push(parsed);
        const { url, method } = parsed;

        if (url.includes("/api/app")) {
          return new Response(
            JSON.stringify({
              mode: "local",
              name: "mlops_agents",
              version: "0.1.0",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/folders")) {
          return new Response(
            JSON.stringify({
              path: "/workspace",
              parent: null,
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.endsWith("/api/github")) {
          return new Response(
            JSON.stringify({
              mode: "gh",
              gh_available: true,
              login: "octocat",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/github/repos")) {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/api/projects") && method === "POST") {
          return addPromise;
        }
        return new Response(null, { status: 404 });
      });
    vi.stubGlobal("fetch", fakeFetch);

    const user = userEvent.setup();
    renderPage("/projects/new", { routes: testRoutes });

    await user.click(await screen.findByRole("tab", { name: "GitHub" }));

    const repoInput = await screen.findByRole("textbox", { name: "Repository" });
    await user.type(repoInput, "octocat/hello-world");

    const addBtn = screen.getByRole("button", { name: "Add" });
    await user.click(addBtn);

    // Progress text shown and button disabled
    expect(await screen.findByText("Downloading octocat/hello-world...")).toBeInTheDocument();
    expect(addBtn).toBeDisabled();

    // Check request body
    const postReq = requests.find((r) => r.url.includes("/api/projects") && r.method === "POST");
    expect(postReq?.body).toEqual({
      source: "github",
      repo: "octocat/hello-world",
    });

    // Resolve mutation
    resolveAdd(
      new Response(
        JSON.stringify({
          id: "prj_gh_123",
          name: "hello-world",
          source: "github",
          status: "ok",
          status_detail: null,
          created_at: "2026-10-21T12:00:00Z",
          last_opened_at: null,
          local_path: null,
          github_full_name: "octocat/hello-world",
          default_branch: "main",
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    // Navigates to project page
    expect(await screen.findByText("Project Page Content")).toBeInTheDocument();
  });

  it("adding a non-git folder shows the dialog and the retry sends init_git: true", async () => {
    const requests: { url: string; method: string; body: unknown }[] = [];
    let addCount = 0;

    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const parsed = await getUrlAndMethod(input, init);
        requests.push(parsed);
        const { url, method } = parsed;

        if (url.includes("/api/app")) {
          return new Response(
            JSON.stringify({
              mode: "local",
              name: "mlops_agents",
              version: "0.1.0",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/folders")) {
          return new Response(
            JSON.stringify({
              path: "/workspace/newbie",
              parent: "/workspace",
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.endsWith("/api/projects") && method === "POST") {
          addCount++;
          if (addCount === 1) {
            return new Response(
              JSON.stringify({
                code: "not_a_git_repo",
                message:
                  "/workspace/newbie is not a git repository. The app can run git init in it for you.",
                detail: { can_init: true },
              }),
              { status: 422, headers: { "Content-Type": "application/json" } },
            );
          }
          return new Response(
            JSON.stringify({
              id: "prj_local_1",
              name: "newbie",
              source: "local",
              status: "ok",
              status_detail: null,
              created_at: "2026-10-21T12:00:00Z",
              last_opened_at: null,
              local_path: "/workspace/newbie",
              github_full_name: null,
              default_branch: "main",
            }),
            { status: 201, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      });
    vi.stubGlobal("fetch", fakeFetch);

    const user = userEvent.setup();
    renderPage("/projects/new", { routes: testRoutes });

    const chooseBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseBtn);

    // Dialog appears with message and "Run git init" button
    expect(
      await screen.findByText(
        "/workspace/newbie is not a git repository. The app can run git init in it for you.",
      ),
    ).toBeInTheDocument();

    const runInitBtn = screen.getByRole("button", { name: "Run git init" });
    await user.click(runInitBtn);

    // Verify retry sent init_git: true
    await waitFor(() => {
      const secondPost = requests.filter(
        (r) => r.url.includes("/api/projects") && r.method === "POST",
      )[1];
      expect(secondPost?.body).toEqual({
        source: "local",
        path: "/workspace/newbie",
        init_git: true,
      });
    });

    expect(await screen.findByText("Project Page Content")).toBeInTheDocument();
  });

  it("not_repo_root offers the root and adding it sends that path", async () => {
    const requests: { url: string; method: string; body: unknown }[] = [];
    let addCount = 0;

    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const parsed = await getUrlAndMethod(input, init);
        requests.push(parsed);
        const { url, method } = parsed;

        if (url.includes("/api/app")) {
          return new Response(
            JSON.stringify({
              mode: "local",
              name: "mlops_agents",
              version: "0.1.0",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/folders")) {
          return new Response(
            JSON.stringify({
              path: "/workspace/repo/subdir",
              parent: "/workspace/repo",
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.endsWith("/api/projects") && method === "POST") {
          addCount++;
          if (addCount === 1) {
            return new Response(
              JSON.stringify({
                code: "not_repo_root",
                message:
                  "This folder is inside the repository /workspace/repo. Add that folder instead.",
                detail: { repo_root: "/workspace/repo" },
              }),
              { status: 422, headers: { "Content-Type": "application/json" } },
            );
          }
          return new Response(
            JSON.stringify({
              id: "prj_local_2",
              name: "repo",
              source: "local",
              status: "ok",
              status_detail: null,
              created_at: "2026-10-21T12:00:00Z",
              last_opened_at: null,
              local_path: "/workspace/repo",
              github_full_name: null,
              default_branch: "main",
            }),
            { status: 201, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      });
    vi.stubGlobal("fetch", fakeFetch);

    const user = userEvent.setup();
    renderPage("/projects/new", { routes: testRoutes });

    const chooseBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseBtn);

    expect(
      await screen.findByText(
        "This folder is inside the repository /workspace/repo. Add that folder instead.",
      ),
    ).toBeInTheDocument();

    const addRootBtn = screen.getByRole("button", { name: "Add /workspace/repo instead" });
    await user.click(addRootBtn);

    await waitFor(() => {
      const secondPost = requests.filter(
        (r) => r.url.includes("/api/projects") && r.method === "POST",
      )[1];
      expect(secondPost?.body).toEqual({
        source: "local",
        path: "/workspace/repo",
        init_git: false,
      });
    });

    expect(await screen.findByText("Project Page Content")).toBeInTheDocument();
  });

  it("project_exists links to the existing project", async () => {
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const { url, method } = await getUrlAndMethod(input, init);

        if (url.includes("/api/app")) {
          return new Response(
            JSON.stringify({
              mode: "local",
              name: "mlops_agents",
              version: "0.1.0",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/folders")) {
          return new Response(
            JSON.stringify({
              path: "/workspace/existing",
              parent: "/workspace",
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.endsWith("/api/projects") && method === "POST") {
          return new Response(
            JSON.stringify({
              code: "project_exists",
              message: "This repository is already the project existing.",
              detail: { project_id: "prj_existing_99" },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      });
    vi.stubGlobal("fetch", fakeFetch);

    const user = userEvent.setup();
    renderPage("/projects/new", { routes: testRoutes });

    const chooseBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseBtn);

    expect(
      await screen.findByText("This repository is already the project existing."),
    ).toBeInTheDocument();

    const openLink = screen.getByRole("link", { name: "Open it" });
    expect(openLink).toHaveAttribute("href", "/projects/prj_existing_99");
  });

  it("adding a local folder shows progress text, disables submit, and prevents double submission", async () => {
    let resolveAdd!: (res: Response) => void;
    const addPromise = new Promise<Response>((resolve) => {
      resolveAdd = resolve;
    });

    const requests: { url: string; method: string; body: unknown }[] = [];

    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const parsed = await getUrlAndMethod(input, init);
        requests.push(parsed);
        const { url, method } = parsed;

        if (url.includes("/api/app")) {
          return new Response(
            JSON.stringify({
              mode: "local",
              name: "mlops_agents",
              version: "0.1.0",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/folders")) {
          return new Response(
            JSON.stringify({
              path: "/workspace/local-project",
              parent: "/workspace",
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.endsWith("/api/projects") && method === "POST") {
          return addPromise;
        }
        return new Response(null, { status: 404 });
      });
    vi.stubGlobal("fetch", fakeFetch);

    const user = userEvent.setup();
    renderPage("/projects/new", { routes: testRoutes });

    const chooseBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseBtn);

    // Progress text shown on button and in tab
    expect(screen.getAllByText("Adding project...").length).toBeGreaterThanOrEqual(1);
    expect(chooseBtn).toBeDisabled();

    // Check request count
    const postRequests = requests.filter(
      (r) => r.url.endsWith("/api/projects") && r.method === "POST",
    );
    expect(postRequests.length).toBe(1);

    // Try clicking again while pending
    await user.click(chooseBtn);
    const postRequestsAfter = requests.filter(
      (r) => r.url.endsWith("/api/projects") && r.method === "POST",
    );
    expect(postRequestsAfter.length).toBe(1);

    // Resolve mutation
    resolveAdd(
      new Response(
        JSON.stringify({
          id: "prj_local_123",
          name: "local-project",
          source: "local",
          status: "ok",
          status_detail: null,
          created_at: "2026-10-21T12:00:00Z",
          last_opened_at: null,
          local_path: "/workspace/local-project",
          github_full_name: null,
          default_branch: "main",
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    // Navigates to project page
    expect(await screen.findByText("Project Page Content")).toBeInTheDocument();
  });
});
