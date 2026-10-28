import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { hostedEmbedderWarning } from "@/lib/models";
import { EmbedderSelect } from "./EmbedderSelect";

type ModelEntry = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];

const groqHosted: Provider = {
  id: "prv_groq",
  name: "groq",
  kind: "hosted",
  base_url: "https://api.groq.com/openai/v1",
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
  keys: [{ position: 1, hint: "gsk_...abcd" }],
};

const fakeSelfHosted: Provider = {
  id: "prv_fake",
  name: "fake",
  kind: "self_hosted",
  base_url: "http://localhost:8000",
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
  keys: [],
};

const usableEmbedder: ModelEntry = {
  address: "fake:fake-embed",
  model_type: "embedder",
  provider_id: "prv_fake",
  provider_name: "fake",
  wire_model_id: "fake-embed",
  context_window: null,
  dimensions: 16,
  tool_calling: false,
  streaming: false,
  availability: "verified",
  availability_detail: null,
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const hostedEmbedder: ModelEntry = {
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

const chatModel: ModelEntry = {
  address: "groq:llama-3",
  model_type: "chat",
  provider_id: "prv_groq",
  provider_name: "groq",
  wire_model_id: "llama-3",
  context_window: 8192,
  dimensions: null,
  tool_calling: true,
  streaming: true,
  availability: "verified",
  availability_detail: null,
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const brokenEmbedder: ModelEntry = {
  address: "fake:broken-embed",
  model_type: "embedder",
  provider_id: "prv_fake",
  provider_name: "fake",
  wire_model_id: "broken-embed",
  context_window: null,
  dimensions: null,
  tool_calling: false,
  streaming: false,
  availability: "needs_attention",
  availability_detail: "Failed to embed",
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: "The embedder fake:broken-embed has not passed verification.",
};

const allModels = [usableEmbedder, hostedEmbedder, chatModel, brokenEmbedder];
const allProviders = [fakeSelfHosted, groqHosted];

describe("EmbedderSelect", () => {
  it("lists only usable embedders and never chat models or broken models", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();

    render(
      <EmbedderSelect
        label="Default embedder"
        noneLabel="None: search uses full text only"
        value={null}
        onChange={onChange}
        models={allModels}
        providers={allProviders}
      />,
    );

    const trigger = await screen.findByRole("combobox", { name: "Default embedder" });
    await user.click(trigger);

    const options = screen.getAllByRole("option").map((opt) => opt.textContent);
    expect(options).toEqual([
      "None: search uses full text only",
      "fake:fake-embed",
      "groq:text-embedding-3-small",
    ]);
    expect(options).not.toContain("groq:llama-3");
    expect(options).not.toContain("fake:broken-embed");
  });

  it("shows 'None' option with provided label and triggers onChange with null", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();

    render(
      <EmbedderSelect
        label="Embedder for this project"
        noneLabel="Use the app default"
        value="fake:fake-embed"
        onChange={onChange}
        models={allModels}
        providers={allProviders}
      />,
    );

    const trigger = await screen.findByRole("combobox", { name: "Embedder for this project" });
    expect(trigger).toHaveTextContent("fake:fake-embed");

    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "Use the app default" }));

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("selecting an option triggers onChange with the model address", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();

    render(
      <EmbedderSelect
        label="Default embedder"
        noneLabel="None: search uses full text only"
        value={null}
        onChange={onChange}
        models={allModels}
        providers={allProviders}
      />,
    );

    const trigger = await screen.findByRole("combobox", { name: "Default embedder" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "fake:fake-embed" }));

    expect(onChange).toHaveBeenCalledWith("fake:fake-embed");
  });

  it("shows hosted warning when selected model belongs to a hosted provider, hides it for self-hosted", () => {
    const { rerender } = render(
      <EmbedderSelect
        label="Default embedder"
        noneLabel="None: search uses full text only"
        value="groq:text-embedding-3-small"
        onChange={vi.fn()}
        models={allModels}
        providers={allProviders}
      />,
    );

    expect(screen.getByText(hostedEmbedderWarning("groq"))).toBeInTheDocument();

    rerender(
      <EmbedderSelect
        label="Default embedder"
        noneLabel="None: search uses full text only"
        value="fake:fake-embed"
        onChange={vi.fn()}
        models={allModels}
        providers={allProviders}
      />,
    );

    expect(screen.queryByText(hostedEmbedderWarning("groq"))).not.toBeInTheDocument();
  });

  it("shows (not usable) for an unusable currently selected address", async () => {
    const user = userEvent.setup();

    render(
      <EmbedderSelect
        label="Default embedder"
        noneLabel="None: search uses full text only"
        value="fake:broken-embed"
        onChange={vi.fn()}
        models={allModels}
        providers={allProviders}
      />,
    );

    const trigger = await screen.findByRole("combobox", { name: "Default embedder" });
    expect(trigger).toHaveTextContent("fake:broken-embed (not usable)");

    await user.click(trigger);
    const options = screen.getAllByRole("option").map((opt) => opt.textContent);
    expect(options).toContain("fake:broken-embed (not usable)");
  });

  it("shows (not usable) for a deleted currently selected address", async () => {
    const user = userEvent.setup();

    render(
      <EmbedderSelect
        label="Default embedder"
        noneLabel="None: search uses full text only"
        value="deleted:old-embed"
        onChange={vi.fn()}
        models={allModels}
        providers={allProviders}
      />,
    );

    const trigger = await screen.findByRole("combobox", { name: "Default embedder" });
    expect(trigger).toHaveTextContent("deleted:old-embed (not usable)");

    await user.click(trigger);
    const options = screen.getAllByRole("option").map((opt) => opt.textContent);
    expect(options).toContain("deleted:old-embed (not usable)");
  });
});
