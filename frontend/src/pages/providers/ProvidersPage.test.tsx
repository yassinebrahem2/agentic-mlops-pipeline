import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import { renderPage } from "../../test/render";

type Provider = components["schemas"]["Provider"];
type DetectedKey = components["schemas"]["DetectedKey"];
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
    "GET /api/providers": () => json([]),
    "GET /api/env-keys": () => json([]),
    "GET /api/models": () => json([]),
    "GET /api/agents": () => json([]),
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      let body: unknown = undefined;
      if (input.method !== "GET" && input.method !== "HEAD") {
        const text = await input.text();
        body = text ? (JSON.parse(text) as unknown) : undefined;
      }
      const request: Sent = {
        method: input.method,
        url: new URL(input.url),
        body,
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

function writes(sent: Sent[]): Sent[] {
  return sent.filter((s) => s.method !== "GET");
}

const groq: Provider = {
  id: "prv_1",
  name: "groq",
  kind: "hosted",
  base_url: "https://api.groq.com/openai/v1",
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
  keys: [
    { position: 1, hint: "gsk_...abcd" },
    { position: 2, hint: "gsk_...wxyz" },
  ],
};

const ollama: Provider = {
  id: "prv_2",
  name: "ollama",
  kind: "self_hosted",
  base_url: "http://localhost:11434/v1",
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
  keys: [],
};

const fakeKey = "sk-test-not-a-real-key";

afterEach(() => {
  vi.unstubAllGlobals();
});

async function openAddProvider() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Add provider" }));
  return { user, dialog: await screen.findByRole("dialog") };
}

describe("AI providers page", () => {
  it("lists the providers with their kind, base URL and masked keys", async () => {
    stubApi({ "GET /api/providers": () => json([groq, ollama]) });
    renderPage("/providers");

    expect(await screen.findByRole("heading", { name: "groq" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "ollama" })).toBeInTheDocument();
    expect(screen.getByText("Hosted")).toBeInTheDocument();
    expect(screen.getByText("Self-hosted")).toBeInTheDocument();
    expect(screen.getByText("https://api.groq.com/openai/v1")).toBeInTheDocument();
    expect(screen.getByText("gsk_...abcd")).toBeInTheDocument();
    expect(screen.getByText("gsk_...wxyz")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove key gsk_...abcd" })).toBeInTheDocument();
  });

  it("adds a self-hosted provider without a key", async () => {
    const sent = stubApi({ "POST /api/providers": () => json(ollama, 201) });
    renderPage("/providers");
    const { user, dialog } = await openAddProvider();

    await user.type(within(dialog).getByLabelText("Name"), "ollama");
    await user.click(within(dialog).getByRole("radio", { name: "Self-hosted" }));
    expect(
      within(dialog).getByText("for example http://localhost:11434/v1 for Ollama"),
    ).toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/First key/)).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Base URL"), "http://localhost:11434/v1");
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(writes(sent)).toEqual([
      {
        method: "POST",
        url: new URL("/api/providers", window.location.origin),
        body: { name: "ollama", kind: "self_hosted", base_url: "http://localhost:11434/v1" },
      },
    ]);
  });

  it("shows the validator's sentence under the base URL field on a 422", async () => {
    const sentence = "base_url must start with http:// or https://";
    stubApi({
      "POST /api/providers": () =>
        json(
          {
            code: "invalid_request",
            message: "Some fields are missing or invalid.",
            detail: { errors: [{ field: "body.base_url", message: sentence }] },
          },
          422,
        ),
    });
    renderPage("/providers");
    const { user, dialog } = await openAddProvider();

    await user.type(within(dialog).getByLabelText("Name"), "groq");
    await user.type(within(dialog).getByLabelText("Base URL"), "ftp://example.com");
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    const baseUrl = within(dialog).getByLabelText("Base URL");
    await waitFor(() => {
      expect(baseUrl).toHaveAccessibleDescription(sentence);
    });
    expect(baseUrl).toHaveAttribute("aria-invalid", "true");
  });

  it("keeps a hosted provider whose first key is rejected, without a key", async () => {
    let created = false;
    const groqWithoutKeys: Provider = { ...groq, keys: [] };
    const sent = stubApi({
      "GET /api/providers": () => json(created ? [groqWithoutKeys] : []),
      "POST /api/providers": () => {
        created = true;
        return json(groqWithoutKeys, 201);
      },
      "POST /api/providers/groq/keys": () =>
        json({ code: "key_rejected", message: "groq rejected this key.", detail: null }, 422),
    });
    renderPage("/providers");
    const { user, dialog } = await openAddProvider();

    await user.type(within(dialog).getByLabelText("Name"), "groq");
    await user.type(within(dialog).getByLabelText("Base URL"), groq.base_url);
    await user.type(within(dialog).getByLabelText("First key (optional)"), fakeKey);
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    expect(await within(dialog).findByText("groq rejected this key.")).toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      ["POST", "/api/providers", { name: "groq", kind: "hosted", base_url: groq.base_url }],
      ["POST", "/api/providers/groq/keys", { key: fakeKey }],
    ]);

    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    const card = (await screen.findByRole("heading", { name: "groq" })).closest("section");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByRole("button", { name: "Add key" })).toBeVisible();
    expect(within(card as HTMLElement).getByText("No keys yet.")).toBeInTheDocument();
  });

  it("sends a new key once, clears the input and never shows the key again", async () => {
    const withKey: Provider = { ...ollama, keys: [{ position: 1, hint: "sk-...-key" }] };
    const sent = stubApi({
      "GET /api/providers": () => json([ollama]),
      "POST /api/providers/ollama/keys": () => json(withKey),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    await user.click(await screen.findByRole("button", { name: "Add key" }));
    const input = screen.getByLabelText("New key for ollama");
    expect(input).toHaveAttribute("type", "password");
    await user.type(input, fakeKey);
    await user.click(screen.getByRole("button", { name: "Save key" }));

    expect(await screen.findByText("sk-...-key")).toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      ["POST", "/api/providers/ollama/keys", { key: fakeKey }],
    ]);
    expect(screen.queryByLabelText("New key for ollama")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue(fakeKey)).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(fakeKey);
  });

  it("shows the server's message when a new key is rejected", async () => {
    stubApi({
      "GET /api/providers": () => json([ollama]),
      "POST /api/providers/ollama/keys": () =>
        json({ code: "key_rejected", message: "ollama rejected this key.", detail: null }, 422),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    await user.click(await screen.findByRole("button", { name: "Add key" }));
    await user.type(screen.getByLabelText("New key for ollama"), fakeKey);
    await user.click(screen.getByRole("button", { name: "Save key" }));

    expect(await screen.findByText("ollama rejected this key.")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(fakeKey);
  });

  it("removes a key by its current position and renders the returned provider", async () => {
    const afterRemoval: Provider = { ...groq, keys: [{ position: 1, hint: "gsk_...abcd" }] };
    const sent = stubApi({
      "GET /api/providers": () => json([groq]),
      "DELETE /api/providers/groq/keys/2": () => json(afterRemoval),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    await user.click(await screen.findByRole("button", { name: "Remove key gsk_...wxyz" }));

    await waitFor(() => {
      expect(screen.queryByText("gsk_...wxyz")).not.toBeInTheDocument();
    });
    expect(screen.getByText("gsk_...abcd")).toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname])).toEqual([
      ["DELETE", "/api/providers/groq/keys/2"],
    ]);
  });

  it("asks before removing a provider", async () => {
    let removed = false;
    const sent = stubApi({
      "GET /api/providers": () => json(removed ? [] : [groq]),
      "DELETE /api/providers/groq": () => {
        removed = true;
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();
    renderPage("/providers");

    await user.click(await screen.findByRole("button", { name: "Remove provider groq" }));
    const dialog = await screen.findByRole("dialog");
    expect(writes(sent)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Remove provider" }));

    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "groq" })).not.toBeInTheDocument();
    });
    expect(writes(sent).map((s) => [s.method, s.url.pathname])).toEqual([
      ["DELETE", "/api/providers/groq"],
    ]);
  });

  it("shows the detail of a provider that needs attention", async () => {
    stubApi({
      "GET /api/providers": () =>
        json([
          {
            ...groq,
            status: "needs_attention",
            status_detail: "groq rejected the key during a turn.",
          },
        ]),
    });
    renderPage("/providers");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("groq rejected the key during a turn.");
    expect(alert).toHaveTextContent("Fix the key below");
  });

  it("imports keys detected in the environment", async () => {
    const detected: DetectedKey[] = [
      {
        env_var: "GROQ_API_KEY",
        provider_name: "groq",
        base_url: groq.base_url,
        hint: "gsk_...abcd",
        provider_exists: false,
      },
      {
        env_var: "GEMINI_API_KEY",
        provider_name: "gemini",
        base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
        hint: "AIza...1234",
        provider_exists: true,
      },
    ];
    let imported = false;
    const sent = stubApi({
      "GET /api/providers": () => json(imported ? [groq] : []),
      "GET /api/env-keys": () => json(detected),
      "POST /api/env-keys/GROQ_API_KEY/import": () => {
        imported = true;
        return json(groq);
      },
    });
    const user = userEvent.setup();
    renderPage("/providers");

    expect(await screen.findByText("GROQ_API_KEY", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import key GEMINI_API_KEY" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Import GROQ_API_KEY" }));

    expect(await screen.findByRole("heading", { name: "groq" })).toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname])).toEqual([
      ["POST", "/api/env-keys/GROQ_API_KEY/import"],
    ]);
  });

  it("refreshes the page when an import creates the provider but its key is rejected", async () => {
    const detected: DetectedKey = {
      env_var: "GROQ_API_KEY",
      provider_name: "groq",
      base_url: groq.base_url,
      hint: "gsk_...abcd",
      provider_exists: false,
    };
    let created = false;
    stubApi({
      "GET /api/providers": () => json(created ? [{ ...groq, keys: [] }] : []),
      "GET /api/env-keys": () => json([{ ...detected, provider_exists: created }]),
      "POST /api/env-keys/GROQ_API_KEY/import": () => {
        created = true;
        return json(
          { code: "key_rejected", message: "groq rejected this key.", detail: null },
          422,
        );
      },
    });
    const user = userEvent.setup();
    renderPage("/providers");

    await user.click(await screen.findByRole("button", { name: "Import GROQ_API_KEY" }));

    const item = (await screen.findByText("groq rejected this key.")).closest("li");
    expect(item).not.toBeNull();
    expect(item).toHaveTextContent("GROQ_API_KEY");
    const card = (await screen.findByRole("heading", { name: "groq" })).closest("section");
    expect(within(card as HTMLElement).getByRole("button", { name: "Add key" })).toBeVisible();
    expect(
      await screen.findByRole("button", { name: "Import key GROQ_API_KEY" }),
    ).toBeInTheDocument();
  });

  it("says so when the keys in the environment cannot be listed", async () => {
    stubApi({
      "GET /api/env-keys": () =>
        json(
          { code: "keystore_unavailable", message: "The key store is locked.", detail: null },
          503,
        ),
    });
    renderPage("/providers");

    expect(await screen.findByText("The key store is locked.")).toBeInTheDocument();
  });

  it("hides the detected keys section when none are found", async () => {
    stubApi({ "GET /api/providers": () => json([groq]) });
    renderPage("/providers");

    expect(await screen.findByRole("heading", { name: "groq" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Keys found in the environment" }),
    ).not.toBeInTheDocument();
  });
});
