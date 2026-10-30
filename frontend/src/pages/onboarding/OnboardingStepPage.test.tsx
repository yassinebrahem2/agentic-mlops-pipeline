import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import type { PageRoute } from "@/app/routes";
import { setupSessionStreamMock } from "@/pages/agents/session/testing";
import { renderPage } from "@/test/render";
import { route } from "./route";

type Project = components["schemas"]["Project"];
type AgentInfo = components["schemas"]["AgentInfo"];
type SandboxView = components["schemas"]["SandboxView"];

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const project: Project = {
  id: "prj_1",
  name: "hotel-bookings",
  source: "github",
  github_full_name: "acme/hotel-bookings",
  local_path: null,
  default_branch: "main",
  status: "ok",
  status_detail: null,
  created_at: "2026-10-01T00:00:00Z",
  last_opened_at: null,
};

function onboardingAgent(settings: Partial<AgentInfo["settings"]> = {}): AgentInfo {
  return {
    name: "onboarding",
    display_name: "Onboarding & Diagnosis",
    description: "Explores the repository.",
    chat: true,
    tools: [],
    settings: {
      agent: "onboarding",
      display_name: "Onboarding & Diagnosis",
      default_model: "groq/llama",
      project_model: null,
      effective_model: "groq/llama",
      model_problem: null,
      max_steps: null,
      ...settings,
    },
  };
}

function sandbox(docker: Partial<SandboxView["docker"]> = {}): SandboxView {
  return {
    docker: { available: true, detail: null, version: "27.0", ...docker },
    environment: { state: "none", tag: null, detail: null },
    network: false,
  };
}

interface Setup {
  project?: Project;
  agents?: AgentInfo[];
  sandbox?: SandboxView;
  messages?: unknown[];
  failPath?: string;
  postResponse?: () => Response;
}

function AgentsLanding() {
  const location = useLocation();
  return <h1>Agents page {location.search}</h1>;
}

const testRoutes: PageRoute[] = [
  route,
  { path: "/projects/:projectId/agents", element: <AgentsLanding /> },
  { path: "/providers", element: <h1>AI providers page</h1> },
];

function setup(options: Setup = {}) {
  const posted: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      const path = new URL(request.url).pathname;
      if (path === options.failPath) {
        return json({ code: "unavailable", message: `Lookup of ${path} failed.` }, 503);
      }
      if (request.method === "POST") {
        posted.push({ path, body: await request.json() });
        return options.postResponse?.() ?? new Response(null, { status: 204 });
      }
      if (path === "/api/projects/prj_1") return json(options.project ?? project);
      if (path === "/api/projects/prj_1/agents") {
        return json(options.agents ?? [onboardingAgent()]);
      }
      if (path === "/api/projects/prj_1/sandbox") return json(options.sandbox ?? sandbox());
      if (path === "/api/projects/prj_1/agents/onboarding/session") {
        return json({
          id: "s1",
          agent: "onboarding",
          busy: false,
          messages: options.messages ?? [],
          last_seq: 0,
          current_task: null,
        });
      }
      return new Response(null, { status: 404 });
    }),
  );
  renderPage("/projects/prj_1/onboarding", { routes: testRoutes });
  return { posted, user: userEvent.setup() };
}

const DOCKER_SENTENCE = "Without Docker, Onboarding can read the repository but not run commands.";

describe("OnboardingStepPage", () => {
  beforeEach(() => {
    setupSessionStreamMock([]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("with no model, keeps Start disabled and links to AI providers", async () => {
    setup({ agents: [onboardingAgent({ effective_model: null, default_model: null })] });
    expect(
      await screen.findByText("No chat model is set for Onboarding & Diagnosis"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "AI providers" })).toHaveAttribute(
      "href",
      "/providers",
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
    });
  });

  it("shows the model problem's text and keeps Start disabled", async () => {
    setup({
      agents: [onboardingAgent({ model_problem: "The model groq/llama is not verified." })],
    });
    expect(await screen.findByText("The model groq/llama is not verified.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "AI providers" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
    });
  });

  it("without Docker, shows the detail and the sentence and keeps Start enabled", async () => {
    setup({ sandbox: sandbox({ available: false, detail: "Docker daemon is not running." }) });
    expect(await screen.findByText("Docker daemon is not running.")).toBeInTheDocument();
    expect(screen.getByText(DOCKER_SENTENCE)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
    });
  });

  it("with Docker available, shows no sentence", async () => {
    setup();
    expect(await screen.findByText(/Docker is available/)).toBeInTheDocument();
    expect(screen.queryByText(DOCKER_SENTENCE)).not.toBeInTheDocument();
  });

  it("Start posts the first message once and opens Onboarding on the agents page", async () => {
    const { posted, user } = setup();
    const start = await screen.findByRole("button", { name: "Start" });
    await waitFor(() => {
      expect(start).toBeEnabled();
    });
    await user.dblClick(start);

    expect(await screen.findByRole("heading", { name: /Agents page/ })).toHaveTextContent(
      "?agent=onboarding",
    );
    expect(posted).toHaveLength(1);
    expect(posted[0]?.path).toBe("/api/projects/prj_1/agents/onboarding/session/messages");
    const body = posted[0]?.body as { text: string };
    expect(Object.keys(body)).toEqual(["text"]);
    expect(body.text).toContain("hotel-bookings");
    expect(body.text).toContain("acme/hotel-bookings");
  });

  it("names the local path when the project has no GitHub repository", async () => {
    const { posted, user } = setup({
      project: { ...project, source: "local", github_full_name: null, local_path: "/w/hotel" },
    });
    const start = await screen.findByRole("button", { name: "Start" });
    await waitFor(() => {
      expect(start).toBeEnabled();
    });
    await user.click(start);
    await screen.findByRole("heading", { name: /Agents page/ });
    expect((posted[0]?.body as { text: string }).text).toContain("/w/hotel");
  });

  it("a 503 on the post shows its message, stays on the step and lets Start run again", async () => {
    const { posted, user } = setup({
      postResponse: () =>
        json({ code: "provider_unavailable", message: "The model provider is down." }, 503),
    });
    const start = await screen.findByRole("button", { name: "Start" });
    await waitFor(() => {
      expect(start).toBeEnabled();
    });
    await user.click(start);

    expect(await screen.findByRole("alert")).toHaveTextContent("The model provider is down.");
    expect(screen.queryByRole("heading", { name: /Agents page/ })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => {
      expect(posted).toHaveLength(2);
    });
  });

  it("a session that has messages offers Open Onboarding and posts nothing", async () => {
    const { posted, user } = setup({
      messages: [{ id: "m1", role: "user", author: "user", parts: [], created_at: "x" }],
    });
    const open = await screen.findByRole("link", { name: "Open Onboarding" });
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
    await user.click(open);
    expect(await screen.findByRole("heading", { name: /Agents page/ })).toHaveTextContent(
      "?agent=onboarding",
    );
    expect(posted).toHaveLength(0);
  });

  it("a failed project lookup shows its text and no ready state", async () => {
    setup({ failPath: "/api/projects/prj_1" });
    expect(await screen.findByText("Lookup of /api/projects/prj_1 failed.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("a failed session lookup shows its text and keeps Start disabled", async () => {
    const { posted } = setup({ failPath: "/api/projects/prj_1/agents/onboarding/session" });
    expect(
      await screen.findByText("Lookup of /api/projects/prj_1/agents/onboarding/session failed."),
    ).toBeInTheDocument();
    expect(await screen.findByText(/Ready/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
    expect(posted).toHaveLength(0);
  });

  it("a failed agents lookup shows its text, not a ready model", async () => {
    setup({ failPath: "/api/projects/prj_1/agents" });
    expect(
      await screen.findByText("Lookup of /api/projects/prj_1/agents failed."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Ready/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });
});
