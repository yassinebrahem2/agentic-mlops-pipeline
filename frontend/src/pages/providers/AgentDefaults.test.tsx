import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import { hostedModelWarning } from "../../lib/models";
import { renderPage } from "../../test/render";
import { route } from "./route";

type AgentInfo = components["schemas"]["AgentInfo"];
type ModelEntry = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];
type Sent = { method: string; url: URL; body: unknown };
type Handler = (sent: Sent) => Response | Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(handlers: Record<string, Handler>): Sent[] {
  const sent: Sent[] = [];
  const defaults: Record<string, Handler> = {
    "GET /api/providers": () => json([groq, ollama]),
    "GET /api/env-keys": () => json([]),
    "GET /api/models": () => json([groqLlama, ollamaMistral, brokenModel]),
    "GET /api/agents": () => json([orchestrator]),
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const text = await input.text();
      const request: Sent = {
        method: input.method,
        url: new URL(input.url),
        body: text ? (JSON.parse(text) as unknown) : undefined,
      };
      sent.push(request);
      const handler =
        handlers[`${request.method} ${request.url.pathname}`] ??
        defaults[`${request.method} ${request.url.pathname}`];
      if (!handler) throw new Error(`Unexpected ${request.method} ${request.url.pathname}`);
      return handler(request);
    }),
  );
  return sent;
}

const groq: Provider = {
  id: "prv_groq",
  name: "groq",
  kind: "hosted",
  base_url: "https://api.groq.com/openai/v1",
  status: "ok",
  status_detail: null,
  keys: [{ position: 1, hint: "gsk_...abcd" }],
};

const ollama: Provider = {
  id: "prv_ollama",
  name: "ollama",
  kind: "self_hosted",
  base_url: "http://localhost:11434/v1",
  status: "ok",
  status_detail: null,
  keys: [],
};

const groqLlama: ModelEntry = {
  address: "groq:llama-3.3-70b-versatile",
  provider_id: "prv_groq",
  provider_name: "groq",
  wire_model_id: "llama-3.3-70b-versatile",
  context_window: 131072,
  tool_calling: true,
  streaming: true,
  availability: "verified",
  availability_detail: null,
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const ollamaMistral: ModelEntry = {
  address: "ollama:mistral",
  provider_id: "prv_ollama",
  provider_name: "ollama",
  wire_model_id: "mistral",
  context_window: 32768,
  tool_calling: true,
  streaming: true,
  availability: "verified",
  availability_detail: null,
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const brokenModel: ModelEntry = {
  address: "groq:broken-model",
  provider_id: "prv_groq",
  provider_name: "groq",
  wire_model_id: "broken-model",
  context_window: 8192,
  tool_calling: false,
  streaming: false,
  availability: "needs_attention",
  availability_detail: "failed tool call",
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: "The model groq:broken-model failed: failed tool call.",
};

const orchestrator: AgentInfo = {
  name: "orchestrator",
  display_name: "Orchestrator",
  description: "Coordinates other agents and manages stage progress.",
  chat: true,
  tools: ["stage_plan"],
  settings: {
    agent: "orchestrator",
    display_name: "Orchestrator",
    default_model: null,
    effective_model: null,
    project_model: null,
    max_steps: 50,
    model_problem: "Choose a model for Orchestrator to run tasks.",
  },
};

const coder: AgentInfo = {
  name: "coder",
  display_name: "Coder",
  description: "Implements tasks and writes tests.",
  chat: false,
  tools: ["file_editor"],
  settings: {
    agent: "coder",
    display_name: "Coder",
    default_model: "ollama:mistral",
    effective_model: "ollama:mistral",
    project_model: null,
    max_steps: 30,
    model_problem: null,
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AgentDefaults", () => {
  it("the select offers only usable models plus None", async () => {
    const user = userEvent.setup();
    stubApi({
      "GET /api/agents": () => json([orchestrator]),
      "GET /api/models": () => json([groqLlama, ollamaMistral, brokenModel]),
    });
    renderPage("/providers", { routes: [route] });

    const trigger = await screen.findByRole("combobox", { name: "Model for Orchestrator" });
    await user.click(trigger);
    const options = screen.getAllByRole("option").map((opt) => opt.textContent);
    expect(options).toEqual(["None", "groq:llama-3.3-70b-versatile", "ollama:mistral"]);
    expect(options).not.toContain("groq:broken-model");
  });

  it("a current default that is no longer usable appears as '<address> (not usable)' and stays selected", async () => {
    const user = userEvent.setup();
    const agentWithBrokenModel: AgentInfo = {
      ...orchestrator,
      settings: {
        ...orchestrator.settings,
        default_model: "groq:broken-model",
        model_problem: null,
      },
    };
    stubApi({
      "GET /api/agents": () => json([agentWithBrokenModel]),
      "GET /api/models": () => json([groqLlama, ollamaMistral, brokenModel]),
    });
    renderPage("/providers", { routes: [route] });

    const trigger = await screen.findByRole("combobox", { name: "Model for Orchestrator" });
    expect(trigger).toHaveTextContent("groq:broken-model (not usable)");
    await user.click(trigger);
    const options = screen.getAllByRole("option").map((opt) => opt.textContent);
    expect(options).toContain("groq:broken-model (not usable)");
  });

  it("choosing sends the address and the row's problem disappears after refetch", async () => {
    const user = userEvent.setup();
    let currentAgent: AgentInfo = orchestrator;
    const sent = stubApi({
      "GET /api/agents": () => json([currentAgent]),
      "PUT /api/agents/orchestrator/model": (req) => {
        const body = req.body as { address: string | null };
        currentAgent = {
          ...orchestrator,
          settings: {
            ...orchestrator.settings,
            default_model: body.address,
            model_problem: null,
          },
        };
        return json(currentAgent);
      },
    });

    renderPage("/providers", { routes: [route] });

    expect(
      await screen.findByText("Choose a model for Orchestrator to run tasks."),
    ).toBeInTheDocument();

    const trigger = await screen.findByRole("combobox", { name: "Model for Orchestrator" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "groq:llama-3.3-70b-versatile" }));

    const putCalls = sent.filter((s) => s.method === "PUT");
    expect(putCalls).toHaveLength(1);
    expect(putCalls[0]?.url.pathname).toBe("/api/agents/orchestrator/model");
    expect(putCalls[0]?.body).toEqual({ address: "groq:llama-3.3-70b-versatile" });

    await waitFor(() => {
      expect(
        screen.queryByText("Choose a model for Orchestrator to run tasks."),
      ).not.toBeInTheDocument();
    });
  });

  it("choosing None sends null", async () => {
    const user = userEvent.setup();
    const agentWithModel: AgentInfo = {
      ...orchestrator,
      settings: {
        ...orchestrator.settings,
        default_model: "groq:llama-3.3-70b-versatile",
        model_problem: null,
      },
    };
    let currentAgent = agentWithModel;
    const sent = stubApi({
      "GET /api/agents": () => json([currentAgent]),
      "PUT /api/agents/orchestrator/model": (req) => {
        const body = req.body as { address: string | null };
        currentAgent = {
          ...orchestrator,
          settings: {
            ...orchestrator.settings,
            default_model: body.address,
            model_problem: null,
          },
        };
        return json(currentAgent);
      },
    });

    renderPage("/providers", { routes: [route] });

    const trigger = await screen.findByRole("combobox", { name: "Model for Orchestrator" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "None" }));

    const putCalls = sent.filter((s) => s.method === "PUT");
    expect(putCalls).toHaveLength(1);
    expect(putCalls[0]?.body).toEqual({ address: null });
  });

  it("the hosted warning is shown for a hosted model and absent for a self-hosted one", async () => {
    const hostedAgent: AgentInfo = {
      ...orchestrator,
      settings: {
        ...orchestrator.settings,
        default_model: "groq:llama-3.3-70b-versatile",
        model_problem: null,
      },
    };
    stubApi({
      "GET /api/agents": () => json([hostedAgent, coder]),
    });

    renderPage("/providers", { routes: [route] });

    expect(await screen.findByText(hostedModelWarning("groq"))).toBeInTheDocument();
    expect(screen.queryByText(hostedModelWarning("ollama"))).not.toBeInTheDocument();
  });

  it("model_problem is shown when the agent has no model", async () => {
    stubApi({
      "GET /api/agents": () => json([orchestrator]),
    });

    renderPage("/providers", { routes: [route] });

    expect(
      await screen.findByText("Choose a model for Orchestrator to run tasks."),
    ).toBeInTheDocument();
  });

  it("error displayed when mutation fails", async () => {
    const user = userEvent.setup();
    stubApi({
      "GET /api/agents": () => json([orchestrator]),
      "PUT /api/agents/orchestrator/model": () =>
        json(
          {
            code: "model_not_usable",
            message: "Model groq:llama-3.3-70b-versatile is not usable.",
            detail: null,
          },
          400,
        ),
    });

    renderPage("/providers", { routes: [route] });

    const trigger = await screen.findByRole("combobox", { name: "Model for Orchestrator" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "groq:llama-3.3-70b-versatile" }));

    expect(
      await screen.findByText("Model groq:llama-3.3-70b-versatile is not usable."),
    ).toBeInTheDocument();
  });

  it("shows an error when models query fails and does not render rows", async () => {
    stubApi({
      "GET /api/agents": () => json([orchestrator]),
      "GET /api/models": () =>
        json({ code: "server_error", message: "Failed to load models.", detail: null }, 500),
    });

    renderPage("/providers", { routes: [route] });

    const section = await screen.findByRole("region", { name: "Default model per agent" });
    expect(await within(section).findByText("Failed to load models.")).toBeInTheDocument();
    expect(within(section).queryByLabelText("Model for Orchestrator")).not.toBeInTheDocument();
  });

  it("shows an error when providers query fails and does not render rows", async () => {
    stubApi({
      "GET /api/agents": () => json([orchestrator]),
      "GET /api/providers": () =>
        json({ code: "server_error", message: "Failed to load providers.", detail: null }, 500),
    });

    renderPage("/providers", { routes: [route] });

    const section = await screen.findByRole("region", { name: "Default model per agent" });
    expect(await within(section).findByText("Failed to load providers.")).toBeInTheDocument();
    expect(within(section).queryByLabelText("Model for Orchestrator")).not.toBeInTheDocument();
  });
});
