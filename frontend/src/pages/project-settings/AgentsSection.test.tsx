import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { AgentsSection } from "./AgentsSection";
import { json, renderSection, stubRoutes } from "./testing";

type Agent = components["schemas"]["AgentInfo"];
type Model = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];

const groq: Provider = {
  id: "prv_groq",
  name: "groq",
  kind: "hosted",
  base_url: "https://api.groq.com/openai/v1",
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
  keys: [{ position: 1, hint: "gsk_...abcd" }],
};

const chatModel: Model = {
  address: "groq:llama-3.3-70b-versatile",
  model_type: "chat",
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

const brokenChat: Model = {
  address: "groq:broken-model",
  model_type: "chat",
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

const embedderModel: Model = {
  address: "groq:text-embedding-3-small",
  model_type: "embedder",
  provider_id: "prv_groq",
  provider_name: "groq",
  wire_model_id: "text-embedding-3-small",
  context_window: null,
  dimensions: 1536,
  tool_calling: false,
  streaming: false,
  availability: "verified",
  availability_detail: null,
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const orchestrator: Agent = {
  name: "orchestrator",
  display_name: "Orchestrator",
  description: "Coordinates other agents and manages stage progress.",
  chat: true,
  tools: ["stage_plan"],
  settings: {
    agent: "orchestrator",
    display_name: "Orchestrator",
    default_model: "groq:llama-3.3-70b-versatile",
    effective_model: "groq:llama-3.3-70b-versatile",
    project_model: null,
    max_steps: 50,
    model_problem: null,
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AgentsSection", () => {
  it("picker lists only usable chat models and never embedders or broken models", async () => {
    const user = userEvent.setup();
    stubRoutes({
      "GET /api/projects/prj_1/agents": () => json([orchestrator]),
      "GET /api/models": () => json([chatModel, brokenChat, embedderModel]),
      "GET /api/providers": () => json([groq]),
    });

    renderSection(<AgentsSection projectId="prj_1" />);

    const trigger = await screen.findByRole("combobox", { name: "Model" });
    await user.click(trigger);
    const options = screen.getAllByRole("option").map((opt) => opt.textContent);
    expect(options).toEqual([
      "Use the app default (groq:llama-3.3-70b-versatile)",
      "groq:llama-3.3-70b-versatile",
    ]);
    expect(options).not.toContain("groq:broken-model");
    expect(options).not.toContain("groq:text-embedding-3-small");
  });

  it("shows an embedder project override as '(not usable)' and keeps it selected", async () => {
    const user = userEvent.setup();
    const agentWithEmbedderOverride: Agent = {
      ...orchestrator,
      settings: {
        ...orchestrator.settings,
        project_model: "groq:text-embedding-3-small",
        effective_model: "groq:text-embedding-3-small",
        model_problem: "Model groq:text-embedding-3-small is an embedder.",
      },
    };

    stubRoutes({
      "GET /api/projects/prj_1/agents": () => json([agentWithEmbedderOverride]),
      "GET /api/models": () => json([chatModel, embedderModel]),
      "GET /api/providers": () => json([groq]),
    });

    renderSection(<AgentsSection projectId="prj_1" />);

    const trigger = await screen.findByRole("combobox", { name: "Model" });
    expect(trigger).toHaveTextContent("groq:text-embedding-3-small (not usable)");

    await user.click(trigger);
    const options = screen.getAllByRole("option").map((opt) => opt.textContent);
    expect(options).toContain("groq:text-embedding-3-small (not usable)");
  });

  it("choosing a model sends PUT /api/projects/{projectId}/agents/{agent}/model", async () => {
    const user = userEvent.setup();
    let currentAgent = orchestrator;
    const requests = stubRoutes({
      "GET /api/projects/prj_1/agents": () => json([currentAgent]),
      "GET /api/models": () => json([chatModel]),
      "GET /api/providers": () => json([groq]),
      "PUT /api/projects/prj_1/agents/orchestrator/model": (body) => {
        const address = (body as { address: string | null }).address;
        currentAgent = {
          ...orchestrator,
          settings: {
            ...orchestrator.settings,
            project_model: address,
            effective_model: address ?? orchestrator.settings.default_model,
          },
        };
        return json(currentAgent);
      },
    });

    renderSection(<AgentsSection projectId="prj_1" />);

    const trigger = await screen.findByRole("combobox", { name: "Model" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "groq:llama-3.3-70b-versatile" }));

    await waitFor(() => {
      const puts = requests.filter((r) => r.method === "PUT");
      expect(puts).toHaveLength(1);
      expect(puts[0]?.path).toBe("/api/projects/prj_1/agents/orchestrator/model");
      expect(puts[0]?.body).toEqual({ address: "groq:llama-3.3-70b-versatile" });
    });
  });
});
