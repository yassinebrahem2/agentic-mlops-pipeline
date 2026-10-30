import { afterEach, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { components } from "@/api/schema";
import { renderPage } from "@/test/render";
import { route } from "./route";

function json(value: unknown) {
  return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
}

function setup(failedPath: string) {
  const app: components["schemas"]["AppInfo"] = {
    name: "App",
    version: "1",
    mode: "server",
    data_dir: "/data",
  };
  const github: components["schemas"]["GitHubStatus"] = {
    mode: "token",
    gh_available: false,
    login: "owner",
  };
  const project: components["schemas"]["Project"] = {
    id: "prj_new",
    name: "repo",
    source: "github",
    github_full_name: "owner/repo",
    local_path: null,
    default_branch: "main",
    status: "ok",
    status_detail: null,
    created_at: "2026-10-01T00:00:00Z",
    last_opened_at: null,
  };
  const added: unknown[] = [];
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      const path = new URL(request.url).pathname;
      requested.push(path);
      if (path === failedPath) return new Response(null, { status: 503 });
      if (path === "/api/app") return json(app);
      if (path === "/api/github") return json(github);
      if (path === "/api/projects" && request.method === "POST") {
        added.push(await request.json());
        return json(project);
      }
      if (path === "/api/projects") return json([]);
      return new Response(null, { status: 404 });
    }),
  );
  renderPage("/projects/new", {
    routes: [
      route,
      { path: "/projects/:projectId/onboarding", element: <h1>Onboarding step opened</h1> },
    ],
  });
  return { added, requested, user: userEvent.setup() };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

it("shows a failed app lookup without assuming local mode or exposing folder controls", async () => {
  const { requested } = setup("/api/app");
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The server answered with an error (HTTP 503).",
  );
  expect(screen.queryByRole("tab", { name: "Local folder" })).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Folder path" })).not.toBeInTheDocument();
  expect(requested).not.toContain("/api/folders");
});

it("shows a failed GitHub status lookup without claiming GitHub needs connecting", async () => {
  setup("/api/github");
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The server answered with an error (HTTP 503).",
  );
  expect(screen.queryByRole("link", { name: "Integrations" })).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Repository" })).not.toBeInTheDocument();
});

it("shows a failed repositories lookup and still accepts a manually entered repository", async () => {
  const { added, user } = setup("/api/github/repos");
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The server answered with an error (HTTP 503).",
  );
  expect(screen.queryByText("No repositories found")).not.toBeInTheDocument();
  await user.type(screen.getByRole("combobox", { name: "Repository" }), "owner/repo");
  await user.click(screen.getByRole("button", { name: "Add" }));
  expect(
    await screen.findByRole("heading", { name: "Onboarding step opened" }),
  ).toBeInTheDocument();
  expect(added).toEqual([{ source: "github", repo: "owner/repo" }]);
});
