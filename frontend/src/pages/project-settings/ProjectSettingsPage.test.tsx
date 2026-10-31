import { afterEach, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { components } from "@/api/schema";
import { renderPage } from "@/test/render";
import { hostedModelWarning } from "@/lib/models";
import { route } from "./route";
import { stubEventStream } from "./testing";

type Project = components["schemas"]["Project"];
type Agent = components["schemas"]["AgentInfo"];
type Model = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];
const project: Project = {
  id: "prj_demo",
  name: "Demo",
  source: "local",
  local_path: "/old",
  github_full_name: null,
  default_branch: "main",
  status: "missing",
  status_detail: "Moved folder",
  created_at: "2026-10-01T00:00:00Z",
  last_opened_at: null,
};
const agent: Agent = {
  name: "orchestrator",
  display_name: "Orchestrator",
  description: "Coordinates",
  chat: true,
  tools: [],
  settings: {
    agent: "orchestrator",
    display_name: "Orchestrator",
    default_model: "cloud/good",
    project_model: null,
    effective_model: "cloud/good",
    model_problem: null,
    max_steps: 10,
  },
};
const good: Model = {
  address: "cloud/good",
  model_type: "chat",
  provider_name: "cloud",
  provider_id: "provider_id",
  wire_model_id: "good",
  availability: "verified",
  availability_detail: null,
  context_window: 10000,
  streaming: true,
  tool_calling: true,
  verified_at: null,
  problem: null,
};
const provider: Provider = {
  id: "provider_id",
  name: "cloud",
  kind: "hosted",
  base_url: "https://example.com",
  keys: [],
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
};
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function setup(
  options: {
    project?: Project;
    mode?: "local" | "server";
    agent?: Agent;
    models?: Model[];
    provider?: Provider;
    failure?: string;
  } = {},
) {
  stubEventStream();
  let current = options.project ?? project;
  let currentAgent = options.agent ?? agent;
  const requests: { path: string; method: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      const path = new URL(request.url).pathname;
      const method = request.method;
      const body: unknown =
        method === "PUT" || path.endsWith("/relink") ? await request.json() : null;
      requests.push({ path, method, body });
      if (path === "/api/projects") return json([current]);
      if (path === "/api/app")
        return json({
          name: "App",
          mode: options.mode ?? "local",
          version: "1",
          data_dir: "/data",
        });
      if (path === "/api/models")
        return json(
          options.models ?? [good, { ...good, address: "cloud/bad", problem: "Unusable" }],
        );
      if (path === "/api/providers") return json([options.provider ?? provider]);
      if (path === "/api/folders")
        return json({ path: "/new", parent: "/", entries: [], roots: ["/"] });
      if (path.endsWith("/settings/permissions")) return json({ level: "ask" });
      if (path.endsWith("/settings/issues")) return json({ min_priority: "medium" });
      if (path.endsWith("/settings/messaging")) return json({ max_hops: 6, max_tokens: 300000 });
      if (path.endsWith("/sandbox"))
        return json({
          docker: { available: true, version: "27.1", detail: null },
          environment: { state: "ready", tag: "env:1", detail: null },
          network: false,
        });
      if (
        [
          "/connections",
          "/on-call",
          "/hooks",
          "/expectations",
          "/checks",
          "/training/policies",
          "/rules",
          "/maintenance-windows",
        ].some((suffix) => path.endsWith(suffix))
      )
        return json([]);
      if (path.endsWith("/monitoring-settings"))
        return json({
          collection_interval_s: 60,
          grouping_window_minutes: 60,
          reopen_window_hours: 24,
          recent_change_window_hours: 24,
          escalation_timeout_minutes: 15,
          check_grace_minutes: 30,
          second_deadline_minutes: 60,
          parser_disable_after: 3,
          evaluation_max_age_hours: 72,
          circuit_breaker_fixes: 3,
          circuit_breaker_window_days: 30,
          timezone: null,
        });
      if (method !== "GET" && options.failure)
        return json({ code: options.failure, message: "Action failed", detail: null }, 409);
      if (path.endsWith("/refresh") || path.endsWith("/relink")) {
        current = {
          ...current,
          status: "ok",
          status_detail: null,
          local_path: path.endsWith("/relink") ? "/new" : current.local_path,
        };
        return json(current);
      }
      if (method === "DELETE") return new Response(null, { status: 204 });
      if (method === "PUT") {
        if (
          path.endsWith("/model") &&
          body !== null &&
          typeof body === "object" &&
          "address" in body &&
          (typeof body.address === "string" || body.address === null)
        )
          currentAgent = {
            ...currentAgent,
            settings: {
              ...currentAgent.settings,
              project_model: body.address,
              effective_model: body.address ?? currentAgent.settings.default_model,
            },
          };
        if (
          path.endsWith("/limits") &&
          body !== null &&
          typeof body === "object" &&
          "max_steps" in body &&
          (typeof body.max_steps === "number" || body.max_steps === null)
        )
          currentAgent = {
            ...currentAgent,
            settings: { ...currentAgent.settings, max_steps: body.max_steps },
          };
        return json(currentAgent);
      }
      if (path.endsWith("/agents")) return json([currentAgent]);
      if (path === "/api/projects/prj_demo") return json(current);
      return new Response(null, { status: 404 });
    }),
  );
  renderPage("/projects/prj_demo/settings", {
    routes: [route, { path: "/", element: <h1>Projects home</h1> }],
  });
  return { requests, user: userEvent.setup() };
}
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it("shows local repository details and refreshes its status", async () => {
  const { user, requests } = setup();
  expect(await screen.findByText("/old")).toBeInTheDocument();
  expect(screen.getByText("Local folder")).toBeInTheDocument();
  expect(screen.getByText("main")).toBeInTheDocument();
  expect(screen.getByText("Moved folder")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByText("OK")).toBeInTheDocument();
  expect(requests).toContainEqual({
    path: "/api/projects/prj_demo/refresh",
    method: "POST",
    body: null,
  });
});
it.each(["local", "server"] as const)(
  "shows GitHub details without Relink in %s mode",
  async (mode) => {
    setup({
      mode,
      project: { ...project, source: "github", local_path: null, github_full_name: "org/repo" },
    });
    expect(await screen.findByText("org/repo")).toBeInTheDocument();
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Relink" })).not.toBeInTheDocument();
  },
);
it("hides Relink for local projects in server mode", async () => {
  setup({ mode: "server" });
  await screen.findByText("/old");
  expect(screen.queryByRole("button", { name: "Relink" })).not.toBeInTheDocument();
});
it("relinks a local project through the folder browser", async () => {
  const { user, requests } = setup();
  await user.click(await screen.findByRole("button", { name: "Relink" }));
  await user.click(await screen.findByRole("button", { name: "Use this folder" }));
  expect(await screen.findByText("/new")).toBeInTheDocument();
  expect(requests).toContainEqual({
    path: "/api/projects/prj_demo/relink",
    method: "POST",
    body: { path: "/new" },
  });
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
it.each(["cloud/bad", "missing/model"])(
  "keeps current unusable override %s selected while excluding other unusable choices",
  async (address) => {
    const { user } = setup({
      agent: {
        ...agent,
        settings: {
          ...agent.settings,
          project_model: address,
          model_problem: "Choose another model",
        },
      },
    });
    const select = await screen.findByRole("combobox", { name: "Model" });
    expect(select).toHaveTextContent(`${address} (not usable)`);
    await user.click(select);
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Use the app default (cloud/good)",
      "cloud/good",
      `${address} (not usable)`,
    ]);
    expect(screen.getByText("Choose another model")).toBeInTheDocument();
  },
);
it("sends a model address and null when restoring the app default", async () => {
  const { user, requests } = setup();
  const select = await screen.findByRole("combobox", { name: "Model" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "cloud/good" }));
  await waitFor(() => {
    expect(select).toHaveTextContent("cloud/good");
  });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: /^Use the app default/ }));
  await waitFor(() => {
    expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
      { address: "cloud/good" },
      { address: null },
    ]);
  });
});
it("shows none for an absent app default", async () => {
  const { user } = setup({
    agent: {
      ...agent,
      settings: { ...agent.settings, default_model: null, effective_model: null },
    },
  });
  const select = await screen.findByRole("combobox", { name: "Model" });
  expect(select).toHaveTextContent("Use the app default (none)");
  await user.click(select);
  expect(
    await screen.findByRole("option", { name: "Use the app default (none)" }),
  ).toBeInTheDocument();
});
it("saves a numeric step limit and No limit as null", async () => {
  const { user, requests } = setup();
  const input = await screen.findByRole("spinbutton", { name: "Step limit" });
  expect(input).toHaveAttribute("min", "1");
  await user.clear(input);
  await user.type(input, "7");
  await user.click(screen.getByRole("button", { name: "Save step limit" }));
  await waitFor(() => {
    expect(requests).toContainEqual({
      path: "/api/projects/prj_demo/agents/orchestrator/limits",
      method: "PUT",
      body: { max_steps: 7 },
    });
  });
  await user.click(screen.getByRole("switch", { name: "No limit" }));
  await user.click(screen.getByRole("button", { name: "Save step limit" }));
  await waitFor(() => {
    expect(requests).toContainEqual({
      path: "/api/projects/prj_demo/agents/orchestrator/limits",
      method: "PUT",
      body: { max_steps: null },
    });
  });
});
it("looks up the effective model's provider_name for the hosted warning", async () => {
  setup({
    models: [{ ...good, address: "alias/model" }],
    agent: { ...agent, settings: { ...agent.settings, effective_model: "alias/model" } },
  });
  expect(await screen.findByText(hostedModelWarning("cloud"))).toBeInTheDocument();
});
it("does not warn for a self-hosted effective model", async () => {
  setup({ provider: { ...provider, kind: "self_hosted" } });
  await screen.findByRole("combobox", { name: "Model" });
  expect(screen.queryByText(hostedModelWarning("cloud"))).not.toBeInTheDocument();
});
it.each(["local", "github"] as const)(
  "confirms removal of a %s project and returns home",
  async (source) => {
    const { user, requests } = setup({ project: { ...project, source } });
    await user.click(await screen.findByRole("button", { name: "Remove project" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("sessions and settings");
    expect(dialog).toHaveTextContent(
      source === "local" ? "Your own folder is not touched" : "downloaded copy is deleted",
    );
    expect(requests.some((r) => r.method === "DELETE")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Remove project" }));
    expect(await screen.findByRole("heading", { name: "Projects home" })).toBeInTheDocument();
    expect(requests).toContainEqual({
      path: "/api/projects/prj_demo",
      method: "DELETE",
      body: null,
    });
  },
);

it("removes project queries so neither the project nor its agents are fetched again after delete", async () => {
  const { user, requests } = setup();
  await user.click(await screen.findByRole("button", { name: "Remove project" }));
  const dialog = screen.getByRole("dialog");
  await user.click(within(dialog).getByRole("button", { name: "Remove project" }));
  expect(await screen.findByRole("heading", { name: "Projects home" })).toBeInTheDocument();

  const deleteIndex = requests.findIndex(
    (r) => r.method === "DELETE" && r.path === "/api/projects/prj_demo",
  );
  expect(deleteIndex).toBeGreaterThanOrEqual(0);

  const postDeleteRequests = requests.slice(deleteIndex + 1);
  expect(postDeleteRequests.some((r) => r.path === "/api/projects/prj_demo")).toBe(false);
  expect(postDeleteRequests.some((r) => r.path === "/api/projects/prj_demo/agents")).toBe(false);
});
it.each(["not_local", "local_projects_disabled"])(
  "shows the server's relink error (%s)",
  async (failure) => {
    const { user } = setup({ failure });
    await user.click(await screen.findByRole("button", { name: "Relink" }));
    await user.click(await screen.findByRole("button", { name: "Use this folder" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Action failed");
  },
);
it("shows a rejected model choice next to its row", async () => {
  const { user } = setup({ failure: "model_not_usable" });
  const select = await screen.findByRole("combobox", { name: "Model" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "cloud/good" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Action failed");
});

it("keeps the real default selected when there is no override", async () => {
  const { user } = setup();
  const select = await screen.findByRole("combobox", { name: "Model" });
  expect(select).toHaveTextContent("Use the app default (cloud/good)");
  await user.click(select);
  expect(screen.queryByRole("option", { name: "cloud/bad" })).not.toBeInTheDocument();
});

it("allows cancelling removal without deleting the project", async () => {
  const { user, requests } = setup();
  await user.click(await screen.findByRole("button", { name: "Remove project" }));
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(requests.some((request) => request.method === "DELETE")).toBe(false);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("renders the permissions, sandbox, GitHub issues and agent messages sections", async () => {
  setup();
  expect(await screen.findByRole("combobox", { name: "Permission level" })).toHaveTextContent(
    "Ask",
  );
  expect(await screen.findByRole("button", { name: "Rebuild" })).toBeEnabled();
  expect(await screen.findByRole("combobox", { name: "Minimum priority" })).toHaveTextContent(
    "Medium",
  );
  expect(await screen.findByRole("spinbutton", { name: "Maximum messages" })).toHaveValue(6);
  for (const name of ["Permissions", "Sandbox", "Connections", "GitHub issues", "Agent messages"]) {
    expect(screen.getByRole("region", { name })).toBeInTheDocument();
  }
});
