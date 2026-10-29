import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { AddModelDialog } from "./AddModelDialog";

type Provider = components["schemas"]["Provider"];

const groq: Provider = {
  id: "prv_1",
  name: "groq",
  kind: "hosted",
  base_url: "https://api.groq.com/openai/v1",
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
  keys: [{ position: 1, hint: "gsk_...abcd" }],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderDialog(providers: Provider[] = [groq]) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AddModelDialog providers={providers} />
    </QueryClientProvider>,
  );
}

describe("AddModelDialog", () => {
  it("model field is visible and takes typing before listing resolves", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        if (url.includes("/api/providers/groq/remote-models")) {
          // Unresolved promise; no timers
          return new Promise<Response>(() => {});
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      }),
    );

    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Add model" }));
    const providerTrigger = screen.getByRole("combobox", { name: "Provider" });
    await user.click(providerTrigger);
    await user.click(await screen.findByRole("option", { name: "groq" }));

    const modelInput = screen.getByRole("combobox", { name: "Model" });
    expect(modelInput).toBeVisible();
    await user.type(modelInput, "custom-early-model");
    expect(modelInput).toHaveValue("custom-early-model");
  });

  it("after listing resolves, typing filters listed models and arrow selection with Enter picks one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        if (url.includes("/api/providers/groq/remote-models")) {
          return Promise.resolve(
            new Response(
              JSON.stringify([
                { id: "llama-3.3-70b-versatile", context_window: 131072 },
                { id: "openai/gpt-oss-120b", context_window: null },
              ]),
              { headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      }),
    );

    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Add model" }));
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: "groq" }));

    const modelInput = screen.getByRole("combobox", { name: "Model" });
    await user.type(modelInput, "llama");

    expect(
      await screen.findByRole("option", { name: /llama-3\.3-70b-versatile/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /openai\/gpt-oss-120b/ })).not.toBeInTheDocument();

    await user.keyboard("{ArrowDown}{Enter}");
    expect(modelInput).toHaveValue("llama-3.3-70b-versatile");
  });

  it("adding sends chosen or typed model id, chosen provider, and default chat model_type", async () => {
    const postRequests: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input instanceof Request ? input.url : String(input);
        const method = input instanceof Request ? input.method : (init?.method ?? "GET");
        if (url.includes("/api/providers/groq/remote-models")) {
          return new Response(
            JSON.stringify([
              { id: "llama-3.3-70b-versatile", context_window: 131072 },
              { id: "openai/gpt-oss-120b", context_window: null },
            ]),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/models") && method === "POST") {
          let body: unknown;
          if (input instanceof Request) {
            body = await input.clone().json();
          } else if (init?.body) {
            body = JSON.parse(init.body as string);
          }
          postRequests.push(body);
          return new Response(
            JSON.stringify({
              address: "groq:custom-model-id",
              provider_id: "prv_1",
              provider_name: "groq",
              wire_model_id: "custom-model-id",
              context_window: null,
              tool_calling: false,
              streaming: false,
              availability: "untested",
              availability_detail: null,
              verified_at: null,
              model_type: "chat",
              problem: null,
            }),
            { status: 201, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      }),
    );

    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Add model" }));
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: "groq" }));

    const modelInput = screen.getByRole("combobox", { name: "Model" });
    await user.type(modelInput, "custom-model-id");

    await user.click(screen.getByRole("button", { name: "Add model" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    expect(postRequests).toEqual([
      {
        provider: "groq",
        wire_model_id: "custom-model-id",
        model_type: "chat",
      },
    ]);
  });

  it("sends model_type: 'embedder' when Embedder is chosen and displays help text", async () => {
    const postRequests: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input instanceof Request ? input.url : String(input);
        const method = input instanceof Request ? input.method : (init?.method ?? "GET");
        if (url.includes("/api/providers/groq/remote-models")) {
          return new Response(
            JSON.stringify([
              { id: "llama-3.3-70b-versatile", context_window: 131072 },
              { id: "text-embedding-3-small", context_window: null },
            ]),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/models") && method === "POST") {
          let body: unknown;
          if (input instanceof Request) {
            body = await input.clone().json();
          } else if (init?.body) {
            body = JSON.parse(init.body as string);
          }
          postRequests.push(body);
          return new Response(
            JSON.stringify({
              address: "groq:text-embedding-3-small",
              provider_id: "prv_1",
              provider_name: "groq",
              wire_model_id: "text-embedding-3-small",
              context_window: null,
              dimensions: null,
              tool_calling: false,
              streaming: false,
              availability: "untested",
              availability_detail: null,
              verified_at: null,
              model_type: "embedder",
              problem: null,
            }),
            { status: 201, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(null, { status: 404 });
      }),
    );

    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Add model" }));
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: "groq" }));

    // Select Type: Embedder
    const typeTrigger = screen.getByRole("combobox", { name: "Type" });
    await user.click(typeTrigger);
    await user.click(await screen.findByRole("option", { name: "Embedder" }));

    // Help text should be visible
    expect(
      screen.getByText("Any OpenAI-compatible /embeddings endpoint works, hosted or self-hosted."),
    ).toBeInTheDocument();

    const modelInput = screen.getByRole("combobox", { name: "Model" });
    await user.type(modelInput, "text-embedding-3-small");

    await user.click(screen.getByRole("button", { name: "Add model" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    expect(postRequests).toEqual([
      {
        provider: "groq",
        wire_model_id: "text-embedding-3-small",
        model_type: "embedder",
      },
    ]);
  });

  it("resets model ID when type changes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        if (url.includes("/api/providers/groq/remote-models")) {
          return Promise.resolve(
            new Response(JSON.stringify([{ id: "llama-3.3-70b-versatile" }]), {
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      }),
    );

    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Add model" }));
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: "groq" }));

    const modelInput = screen.getByRole("combobox", { name: "Model" });
    await user.type(modelInput, "custom-early-model");
    expect(modelInput).toHaveValue("custom-early-model");

    // Change Type to Embedder
    await user.click(screen.getByRole("combobox", { name: "Type" }));
    await user.click(await screen.findByRole("option", { name: "Embedder" }));

    expect(modelInput).toHaveValue("");
  });

  it("there is no 'Filter models' input and no radio list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        if (url.includes("/api/providers/groq/remote-models")) {
          return Promise.resolve(
            new Response(
              JSON.stringify([{ id: "llama-3.3-70b-versatile", context_window: 131072 }]),
              { headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      }),
    );

    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Add model" }));
    expect(screen.queryByLabelText("Filter models")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Filter models" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });
});
