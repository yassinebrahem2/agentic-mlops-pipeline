import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { EmbedderSection } from "./EmbedderSection";

type ModelEntry = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];
type EmbedderSettingView = components["schemas"]["EmbedderSettingView"];

interface RecordedRequest {
  path: string;
  method: string;
  body: unknown;
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function failure(message: string, status = 409): Response {
  return json({ code: "conflict", message, detail: null }, status);
}

function stubRoutes(
  routes: Record<string, (body: unknown) => Response | Promise<Response>>,
): RecordedRequest[] {
  const requests: RecordedRequest[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      const path = new URL(request.url).pathname;
      const hasBody = request.method === "PUT" || request.method === "POST";
      const text = hasBody ? await request.text() : "";
      const body: unknown = text === "" ? null : JSON.parse(text);
      requests.push({ path, method: request.method, body });
      const handler = routes[`${request.method} ${path}`];
      return handler ? await handler(body) : new Response(null, { status: 404 });
    }),
  );
  return requests;
}

function renderSection(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

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

const defaultSetting: EmbedderSettingView = {
  chosen: "fake:fake-embed",
  status: {
    address: "fake:fake-embed",
    configured: true,
    dimensions: 16,
    problem: null,
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("app-settings EmbedderSection", () => {
  it("renders default embedder select with current value and description", async () => {
    stubRoutes({
      "GET /api/settings/embedder": () => json(defaultSetting),
      "GET /api/models": () => json([fakeEmbed]),
      "GET /api/providers": () => json([fakeProvider]),
    });

    renderSection(<EmbedderSection />);

    expect(await screen.findByRole("heading", { name: "Embedder" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "Notes are searched by meaning with this embedder. With none set, search uses full text only.",
      ),
    ).toBeInTheDocument();

    const trigger = await screen.findByRole("combobox", { name: "Default embedder" });
    expect(trigger).toHaveTextContent("fake:fake-embed");
  });

  it("selecting an embedder sends PUT /api/settings/embedder", async () => {
    const user = userEvent.setup();
    let currentSetting: EmbedderSettingView = {
      chosen: null,
      status: { address: null, configured: false, dimensions: null, problem: null },
    };

    const requests = stubRoutes({
      "GET /api/settings/embedder": () => json(currentSetting),
      "GET /api/models": () => json([fakeEmbed]),
      "GET /api/providers": () => json([fakeProvider]),
      "PUT /api/settings/embedder": (body) => {
        const address = (body as { address: string | null }).address;
        currentSetting = {
          chosen: address,
          status: {
            address,
            configured: address !== null,
            dimensions: address ? 16 : null,
            problem: null,
          },
        };
        return json(currentSetting);
      },
    });

    renderSection(<EmbedderSection />);

    const trigger = await screen.findByRole("combobox", { name: "Default embedder" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "fake:fake-embed" }));

    await waitFor(() => {
      const puts = requests.filter((r) => r.method === "PUT");
      expect(puts).toHaveLength(1);
      expect(puts[0]?.path).toBe("/api/settings/embedder");
      expect(puts[0]?.body).toEqual({ address: "fake:fake-embed" });
    });
  });

  it("selecting None sends PUT /api/settings/embedder with { address: null }", async () => {
    const user = userEvent.setup();
    let currentSetting: EmbedderSettingView = defaultSetting;

    const requests = stubRoutes({
      "GET /api/settings/embedder": () => json(currentSetting),
      "GET /api/models": () => json([fakeEmbed]),
      "GET /api/providers": () => json([fakeProvider]),
      "PUT /api/settings/embedder": (body) => {
        const address = (body as { address: string | null }).address;
        currentSetting = {
          chosen: address,
          status: { address, configured: false, dimensions: null, problem: null },
        };
        return json(currentSetting);
      },
    });

    renderSection(<EmbedderSection />);

    const trigger = await screen.findByRole("combobox", { name: "Default embedder" });
    await user.click(trigger);
    await user.click(
      await screen.findByRole("option", { name: "None: search uses full text only" }),
    );

    await waitFor(() => {
      const puts = requests.filter((r) => r.method === "PUT");
      expect(puts).toHaveLength(1);
      expect(puts[0]?.path).toBe("/api/settings/embedder");
      expect(puts[0]?.body).toEqual({ address: null });
    });
  });

  it("shows status problem when status.problem is present", async () => {
    const problemSetting: EmbedderSettingView = {
      chosen: "fake:broken-embed",
      status: {
        address: "fake:broken-embed",
        configured: true,
        dimensions: null,
        problem: "The embedder fake:broken-embed has not passed verification.",
      },
    };

    stubRoutes({
      "GET /api/settings/embedder": () => json(problemSetting),
      "GET /api/models": () => json([fakeEmbed]),
      "GET /api/providers": () => json([fakeProvider]),
    });

    renderSection(<EmbedderSection />);

    expect(
      await screen.findByText("The embedder fake:broken-embed has not passed verification."),
    ).toBeInTheDocument();
  });

  it("shows mutation error message when PUT fails", async () => {
    const user = userEvent.setup();
    stubRoutes({
      "GET /api/settings/embedder": () => json(defaultSetting),
      "GET /api/models": () => json([fakeEmbed]),
      "GET /api/providers": () => json([fakeProvider]),
      "PUT /api/settings/embedder": () => failure("Model is not usable."),
    });

    renderSection(<EmbedderSection />);

    const trigger = await screen.findByRole("combobox", { name: "Default embedder" });
    await user.click(trigger);
    await user.click(
      await screen.findByRole("option", { name: "None: search uses full text only" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Model is not usable.");
  });
});
