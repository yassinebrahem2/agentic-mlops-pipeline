import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GitHubTab } from "./GitHubTab";

describe("GitHubTab", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function renderGitHubTab() {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <GitHubTab />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("there is no 'Filter repositories' input anymore", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        if (url.includes("/api/github/repos")) {
          return Promise.resolve(
            new Response(JSON.stringify([]), {
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        if (url.includes("/api/github")) {
          return Promise.resolve(
            new Response(JSON.stringify({ mode: "gh", gh_available: true, login: "octocat" }), {
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      }),
    );

    renderGitHubTab();

    expect(await screen.findByRole("combobox", { name: "Repository" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Filter repositories" })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Filter repositories/i)).not.toBeInTheDocument();
  });

  it("typing part of a repo name and pressing Enter fills the full name, and Add sends it", async () => {
    const postRequests: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input instanceof Request ? input.url : String(input);
        const method = input instanceof Request ? input.method : (init?.method ?? "GET");
        if (url.includes("/api/github/repos")) {
          return new Response(
            JSON.stringify([
              {
                full_name: "octocat/hello-world",
                description: "Sample repo",
                private: false,
              },
              {
                full_name: "octocat/second-project",
                description: null,
                private: false,
              },
            ]),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/github")) {
          return new Response(
            JSON.stringify({ mode: "gh", gh_available: true, login: "octocat" }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/projects") && method === "POST") {
          let body: unknown;
          if (input instanceof Request) {
            body = await input.clone().json();
          } else if (init?.body) {
            body = JSON.parse(init.body as string);
          }
          postRequests.push(body);
          return new Response(
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
          );
        }
        return new Response(null, { status: 404 });
      }),
    );

    const user = userEvent.setup();
    renderGitHubTab();

    const repoInput = await screen.findByRole("combobox", { name: "Repository" });

    // Type part of a repo name
    await user.type(repoInput, "hello");

    // Press Enter to fill the full name
    await user.keyboard("{Enter}");
    expect(repoInput).toHaveValue("octocat/hello-world");

    // Click Add
    const addBtn = screen.getByRole("button", { name: "Add" });
    await user.click(addBtn);

    await waitFor(() => {
      expect(postRequests).toEqual([
        {
          source: "github",
          repo: "octocat/hello-world",
        },
      ]);
    });
  });

  it("a pasted URL is sent as typed", async () => {
    const postRequests: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input instanceof Request ? input.url : String(input);
        const method = input instanceof Request ? input.method : (init?.method ?? "GET");
        if (url.includes("/api/github/repos")) {
          return new Response(JSON.stringify([]), {
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/api/github")) {
          return new Response(
            JSON.stringify({ mode: "gh", gh_available: true, login: "octocat" }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/projects") && method === "POST") {
          let body: unknown;
          if (input instanceof Request) {
            body = await input.clone().json();
          } else if (init?.body) {
            body = JSON.parse(init.body as string);
          }
          postRequests.push(body);
          return new Response(
            JSON.stringify({
              id: "prj_gh_url",
              name: "custom-repo",
              source: "github",
              status: "ok",
              status_detail: null,
              created_at: "2026-10-21T12:00:00Z",
              last_opened_at: null,
              local_path: null,
              github_full_name: "octocat/custom-repo",
              default_branch: "main",
            }),
            { status: 201, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      }),
    );

    const user = userEvent.setup();
    renderGitHubTab();

    const repoInput = await screen.findByRole("combobox", { name: "Repository" });
    const pastedUrl = "https://github.com/some-owner/custom-repo";
    await user.type(repoInput, pastedUrl);

    const addBtn = screen.getByRole("button", { name: "Add" });
    await user.click(addBtn);

    await waitFor(() => {
      expect(postRequests).toEqual([
        {
          source: "github",
          repo: pastedUrl,
        },
      ]);
    });
  });
});
