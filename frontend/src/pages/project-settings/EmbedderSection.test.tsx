import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { EmbedderSection } from "./EmbedderSection";
import { failure, json, renderSection, stubRoutes } from "./testing";

type ModelEntry = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];
type EmbedderSettingView = components["schemas"]["EmbedderSettingView"];

const fakeProvider: Provider = {
  id: "prv_fake",
  name: "fake",
  kind: "self_hosted",
  base_url: "http://localhost:8000",
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
  keys: [],
};

const fakeEmbed: ModelEntry = {
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

const wideEmbed: ModelEntry = {
  address: "fake:fake-embed-wide",
  model_type: "embedder",
  provider_id: "prv_fake",
  provider_name: "fake",
  wire_model_id: "fake-embed-wide",
  context_window: null,
  dimensions: 32,
  tool_calling: false,
  streaming: false,
  availability: "verified",
  availability_detail: null,
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const allModels = [fakeEmbed, wideEmbed];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("project-settings EmbedderSection", () => {
  it("renders project embedder select with current override value", async () => {
    const overrideSetting: EmbedderSettingView = {
      chosen: "fake:fake-embed-wide",
      status: {
        address: "fake:fake-embed-wide",
        configured: true,
        dimensions: 32,
        problem: null,
      },
    };

    stubRoutes({
      "GET /api/projects/prj_1/settings/embedder": () => json(overrideSetting),
      "GET /api/models": () => json(allModels),
      "GET /api/providers": () => json([fakeProvider]),
    });

    renderSection(<EmbedderSection projectId="prj_1" />);

    expect(await screen.findByRole("heading", { name: "Embedder" })).toBeInTheDocument();
    const trigger = await screen.findByRole("combobox", { name: "Embedder for this project" });
    expect(trigger).toHaveTextContent("fake:fake-embed-wide");
  });

  it("shows status line 'Using {address}' when active", async () => {
    const defaultInEffect: EmbedderSettingView = {
      chosen: null,
      status: {
        address: "fake:fake-embed",
        configured: true,
        dimensions: 16,
        problem: null,
      },
    };

    stubRoutes({
      "GET /api/projects/prj_1/settings/embedder": () => json(defaultInEffect),
      "GET /api/models": () => json(allModels),
      "GET /api/providers": () => json([fakeProvider]),
    });

    renderSection(<EmbedderSection projectId="prj_1" />);

    const trigger = await screen.findByRole("combobox", { name: "Embedder for this project" });
    expect(trigger).toHaveTextContent("Use the app default");
    expect(await screen.findByText("Using fake:fake-embed")).toBeInTheDocument();
  });

  it("shows 'Using full text only' when unconfigured", async () => {
    const unconfigured: EmbedderSettingView = {
      chosen: null,
      status: {
        address: null,
        configured: false,
        dimensions: null,
        problem: null,
      },
    };

    stubRoutes({
      "GET /api/projects/prj_1/settings/embedder": () => json(unconfigured),
      "GET /api/models": () => json(allModels),
      "GET /api/providers": () => json([fakeProvider]),
    });

    renderSection(<EmbedderSection projectId="prj_1" />);

    expect(await screen.findByText("Using full text only")).toBeInTheDocument();
  });

  it("shows problem in status line when broken", async () => {
    const broken: EmbedderSettingView = {
      chosen: "fake:fake-embed",
      status: {
        address: "fake:fake-embed",
        configured: true,
        dimensions: null,
        problem: "Provider is offline.",
      },
    };

    stubRoutes({
      "GET /api/projects/prj_1/settings/embedder": () => json(broken),
      "GET /api/models": () => json(allModels),
      "GET /api/providers": () => json([fakeProvider]),
    });

    renderSection(<EmbedderSection projectId="prj_1" />);

    expect(
      await screen.findByText("Using full text only: Provider is offline."),
    ).toBeInTheDocument();
  });

  it("selecting an override sends PUT /api/projects/{projectId}/settings/embedder", async () => {
    const user = userEvent.setup();
    let currentSetting: EmbedderSettingView = {
      chosen: null,
      status: { address: "fake:fake-embed", configured: true, dimensions: 16, problem: null },
    };

    const requests = stubRoutes({
      "GET /api/projects/prj_1/settings/embedder": () => json(currentSetting),
      "GET /api/models": () => json(allModels),
      "GET /api/providers": () => json([fakeProvider]),
      "PUT /api/projects/prj_1/settings/embedder": (body) => {
        const address = (body as { address: string | null }).address;
        currentSetting = {
          chosen: address,
          status: {
            address: address ?? "fake:fake-embed",
            configured: true,
            dimensions: address === "fake:fake-embed-wide" ? 32 : 16,
            problem: null,
          },
        };
        return json(currentSetting);
      },
    });

    renderSection(<EmbedderSection projectId="prj_1" />);

    const trigger = await screen.findByRole("combobox", { name: "Embedder for this project" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "fake:fake-embed-wide" }));

    await waitFor(() => {
      const puts = requests.filter((r) => r.method === "PUT");
      expect(puts).toHaveLength(1);
      expect(puts[0]?.path).toBe("/api/projects/prj_1/settings/embedder");
      expect(puts[0]?.body).toEqual({ address: "fake:fake-embed-wide" });
    });
  });

  it("selecting 'Use the app default' sends null", async () => {
    const user = userEvent.setup();
    let currentSetting: EmbedderSettingView = {
      chosen: "fake:fake-embed-wide",
      status: { address: "fake:fake-embed-wide", configured: true, dimensions: 32, problem: null },
    };

    const requests = stubRoutes({
      "GET /api/projects/prj_1/settings/embedder": () => json(currentSetting),
      "GET /api/models": () => json(allModels),
      "GET /api/providers": () => json([fakeProvider]),
      "PUT /api/projects/prj_1/settings/embedder": (body) => {
        const address = (body as { address: string | null }).address;
        currentSetting = {
          chosen: address,
          status: {
            address: address ?? "fake:fake-embed",
            configured: true,
            dimensions: 16,
            problem: null,
          },
        };
        return json(currentSetting);
      },
    });

    renderSection(<EmbedderSection projectId="prj_1" />);

    const trigger = await screen.findByRole("combobox", { name: "Embedder for this project" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "Use the app default" }));

    await waitFor(() => {
      const puts = requests.filter((r) => r.method === "PUT");
      expect(puts).toHaveLength(1);
      expect(puts[0]?.path).toBe("/api/projects/prj_1/settings/embedder");
      expect(puts[0]?.body).toEqual({ address: null });
    });
  });

  it("shows mutation error message when PUT fails", async () => {
    const user = userEvent.setup();
    const currentSetting: EmbedderSettingView = {
      chosen: null,
      status: { address: null, configured: false, dimensions: null, problem: null },
    };

    stubRoutes({
      "GET /api/projects/prj_1/settings/embedder": () => json(currentSetting),
      "GET /api/models": () => json(allModels),
      "GET /api/providers": () => json([fakeProvider]),
      "PUT /api/projects/prj_1/settings/embedder": () => failure("Cannot assign model"),
    });

    renderSection(<EmbedderSection projectId="prj_1" />);

    const trigger = await screen.findByRole("combobox", { name: "Embedder for this project" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "fake:fake-embed" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot assign model");
  });
});
