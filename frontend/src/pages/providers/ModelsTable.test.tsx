import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import { renderPage } from "../../test/render";

type Provider = components["schemas"]["Provider"];
type ModelEntry = components["schemas"]["ModelEntry"];
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
  keys: [{ position: 1, hint: "gsk_...abcd" }],
};

const untested: ModelEntry = {
  address: "fake:fake-tools",
  model_type: "chat",
  provider_id: "prv_2",
  provider_name: "fake",
  wire_model_id: "fake-tools",
  context_window: 32768,
  tool_calling: false,
  streaming: false,
  availability: "untested",
  availability_detail: null,
  verified_at: null,
  problem: "The model fake:fake-tools has not passed verification (tool calls and streaming).",
};

const verified: ModelEntry = {
  ...untested,
  tool_calling: true,
  streaming: true,
  availability: "verified",
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const slashed: ModelEntry = {
  address: "groq:openai/gpt-oss-120b",
  model_type: "chat",
  provider_id: "prv_1",
  provider_name: "groq",
  wire_model_id: "openai/gpt-oss-120b",
  context_window: null,
  tool_calling: true,
  streaming: false,
  availability: "needs_attention",
  availability_detail: "the stream ended without a tool call",
  verified_at: "2026-10-20T08:30:00+00:00",
  problem: "The model groq:openai/gpt-oss-120b failed: the stream ended without a tool call.",
};

const untestedEmbedder: ModelEntry = {
  address: "fake:fake-embed",
  model_type: "embedder",
  provider_id: "prv_2",
  provider_name: "fake",
  wire_model_id: "fake-embed",
  context_window: null,
  dimensions: null,
  tool_calling: false,
  streaming: false,
  availability: "untested",
  availability_detail: null,
  verified_at: null,
  problem: "The embedder fake:fake-embed has not passed verification.",
};

const verifiedEmbedder: ModelEntry = {
  ...untestedEmbedder,
  address: "fake:verified-embed",
  wire_model_id: "verified-embed",
  dimensions: 1536,
  availability: "verified",
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

async function findRow(address: string): Promise<HTMLElement> {
  const cell = await screen.findByRole("cell", { name: (n) => n.includes(address) });
  const row = cell.closest("tr");
  if (!row) throw new Error(`No row for ${address}`);
  return row;
}

describe("models table", () => {
  it("shows each model's traits and availability", async () => {
    stubApi({ "GET /api/models": () => json([verified, slashed]) });
    renderPage("/providers");

    const fake = await findRow("fake:fake-tools");
    expect(within(fake).getAllByText("Yes")).toHaveLength(2);
    expect(within(fake).getByText("32768")).toBeInTheDocument();
    expect(within(fake).getByText("Verified")).toBeInTheDocument();

    const gpt = await findRow("groq:openai/gpt-oss-120b");
    expect(within(gpt).getByText("Yes")).toBeInTheDocument();
    expect(within(gpt).getByText("No")).toBeInTheDocument();
    expect(within(gpt).getByText("Needs attention")).toBeInTheDocument();
    expect(within(gpt).getByText("the stream ended without a tool call")).toBeInTheDocument();
  });

  it("disables Verify while the check runs and shows the result", async () => {
    let answer: (response: Response) => void = () => undefined;
    const sent = stubApi({
      "GET /api/models": () => json([untested]),
      "POST /api/models/verify": () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("fake:fake-tools");
    expect(within(row).getByText("Untested")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Verify fake:fake-tools" }));

    const pending = await within(row).findByRole("button", { name: "Verifying fake:fake-tools" });
    expect(pending).toBeDisabled();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      ["POST", "/api/models/verify", { address: "fake:fake-tools" }],
    ]);

    answer(json(verified));
    expect(await within(row).findByText("Verified")).toBeInTheDocument();
    expect(within(row).getAllByText("Yes")).toHaveLength(2);
    expect(within(row).getByRole("button", { name: "Verify fake:fake-tools" })).toBeEnabled();
  });

  it("shows a failed verification on the row", async () => {
    stubApi({
      "GET /api/models": () => json([untested]),
      "POST /api/models/verify": () =>
        json(
          {
            code: "verification_failed",
            message: "Checking fake:fake-tools failed: the model did not call the tool.",
            detail: null,
          },
          502,
        ),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("fake:fake-tools");
    await user.click(within(row).getByRole("button", { name: "Verify fake:fake-tools" }));

    expect(
      await within(row).findByText(
        "Checking fake:fake-tools failed: the model did not call the tool.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the validator's message under the context window input", async () => {
    const sentence = "Input should be a valid integer, got a number with a fractional part";
    const sent = stubApi({
      "GET /api/models": () => json([slashed]),
      "PUT /api/models/context-window": () =>
        json(
          {
            code: "invalid_request",
            message: "Some fields are missing or invalid.",
            detail: { errors: [{ field: "body.value", message: sentence }] },
          },
          422,
        ),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("groq:openai/gpt-oss-120b");
    await user.click(within(row).getByRole("button", { name: "Set" }));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByLabelText(/^Context window/);
    await user.type(input, "1.5");
    await user.click(within(dialog).getByRole("button", { name: /^Save/ }));

    await waitFor(() => {
      expect(input).toHaveAccessibleDescription(sentence);
    });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(writes(sent).map((s) => s.body)).toEqual([
      { address: "groq:openai/gpt-oss-120b", value: 1.5 },
    ]);
  });

  it("asks for an unknown context window and sends the address with the value", async () => {
    let saved = false;
    const sent = stubApi({
      "GET /api/models": () =>
        json(saved ? [{ ...slashed, context_window: 131072, problem: null }] : [slashed]),
      "PUT /api/models/context-window": () => {
        saved = true;
        return json({ ...slashed, context_window: 131072, problem: null });
      },
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("groq:openai/gpt-oss-120b");
    expect(
      within(row).queryByText(
        "This model cannot be assigned to an agent until its context window is set.",
      ),
    ).not.toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Set" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Context window/), "131072");
    await user.click(within(dialog).getByRole("button", { name: /^Save/ }));

    expect(await within(row).findByText("131072")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Set" })).not.toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      ["PUT", "/api/models/context-window", { address: "groq:openai/gpt-oss-120b", value: 131072 }],
    ]);
  });

  it("warns above the table about models with problems and marks only their rows", async () => {
    stubApi({ "GET /api/models": () => json([verified, slashed]) });
    renderPage("/providers");

    const problem = slashed.problem ?? "";
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(problem);

    const slashedRow = await findRow(slashed.address);
    expect(within(slashedRow).getByRole("img", { name: problem })).toBeInTheDocument();
    expect(within(slashedRow).getByLabelText(problem)).toBeInTheDocument();

    const verifiedRow = await findRow(verified.address);
    expect(within(verifiedRow).queryByRole("img")).not.toBeInTheDocument();
    expect(within(verifiedRow).queryByLabelText(problem)).not.toBeInTheDocument();
  });

  it("does not render a warning alert when no models have problems", async () => {
    stubApi({ "GET /api/models": () => json([verified]) });
    renderPage("/providers");

    await findRow(verified.address);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("pre-fills the provider's listed context window and hides warning after save", async () => {
    const unconfigured: ModelEntry = {
      ...verified,
      address: "groq:openai/gpt-oss-120b",
      wire_model_id: "openai/gpt-oss-120b",
      provider_name: "groq",
      context_window: null,
      problem:
        "The context window of groq:openai/gpt-oss-120b is unknown. Enter it in AI providers.",
    };
    let saved = false;
    const sent = stubApi({
      "GET /api/models": () =>
        json(
          saved
            ? [verified, { ...unconfigured, context_window: 131072, problem: null }]
            : [verified, unconfigured],
        ),
      "GET /api/providers/groq/remote-models": () =>
        json([{ id: "openai/gpt-oss-120b", context_window: 131072 }]),
      "PUT /api/models/context-window": () => {
        saved = true;
        return json({ ...unconfigured, context_window: 131072, problem: null });
      },
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const problem = unconfigured.problem ?? "";
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(problem);

    const row = await findRow("groq:openai/gpt-oss-120b");
    expect(within(row).getByLabelText(problem)).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Set" }));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByLabelText(/^Context window/);
    await waitFor(() => {
      expect(input).toHaveValue(131072);
    });

    await user.click(within(dialog).getByRole("button", { name: /^Save/ }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(await within(row).findByText("131072")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      ["PUT", "/api/models/context-window", { address: "groq:openai/gpt-oss-120b", value: 131072 }],
    ]);
  });

  it("removes a model by sending its address in the query string", async () => {
    let removed = false;
    const sent = stubApi({
      "GET /api/models": () => json(removed ? [] : [slashed]),
      "DELETE /api/models": () => {
        removed = true;
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("groq:openai/gpt-oss-120b");
    await user.click(within(row).getByRole("button", { name: "Remove groq:openai/gpt-oss-120b" }));

    await waitFor(() => {
      expect(
        screen.queryByRole("cell", { name: (n) => n.includes("groq:openai/gpt-oss-120b") }),
      ).toBeNull();
    });
    const removal = writes(sent);
    expect(removal).toHaveLength(1);
    expect(removal[0]?.method).toBe("DELETE");
    expect(removal[0]?.url.pathname).toBe("/api/models");
    expect(removal[0]?.url.searchParams.get("address")).toBe("groq:openai/gpt-oss-120b");
  });

  it("adds a model chosen from the provider's filtered remote list", async () => {
    const sent = stubApi({
      "GET /api/providers": () => json([groq]),
      "GET /api/providers/groq/remote-models": () =>
        json([
          { id: "llama-3.3-70b-versatile", context_window: 131072 },
          { id: "openai/gpt-oss-120b", context_window: null },
        ]),
      "POST /api/models": () => json(slashed, 201),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    await user.click(await screen.findByRole("button", { name: "Add model" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: "groq" }));

    const modelInput = within(dialog).getByRole("combobox", { name: "Model" });
    await user.type(modelInput, "gpt");
    await user.click(await screen.findByRole("option", { name: /openai\/gpt-oss-120b/ }));
    expect(modelInput).toHaveValue("openai/gpt-oss-120b");
    await user.click(within(dialog).getByRole("button", { name: "Add model" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      [
        "POST",
        "/api/models",
        {
          provider: "groq",
          wire_model_id: "openai/gpt-oss-120b",
          model_type: "chat",
        },
      ],
    ]);
  });

  it("lets the user type a model ID when the provider's listing fails", async () => {
    const sent = stubApi({
      "GET /api/providers": () => json([{ ...groq, keys: [] }]),
      "GET /api/providers/groq/remote-models": () =>
        json({ code: "no_keys", message: "Add a key to groq first.", detail: null }, 409),
      "POST /api/models": () => json(slashed, 201),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    await user.click(await screen.findByRole("button", { name: "Add model" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: "groq" }));

    expect(await within(dialog).findByText("Add a key to groq first.")).toBeInTheDocument();
    await user.type(within(dialog).getByRole("combobox", { name: "Model" }), "openai/gpt-oss-120b");
    await user.type(within(dialog).getByLabelText("Context window (optional)"), "131072");
    await user.click(within(dialog).getByRole("button", { name: "Add model" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      [
        "POST",
        "/api/models",
        {
          provider: "groq",
          wire_model_id: "openai/gpt-oss-120b",
          context_window: 131072,
          model_type: "chat",
        },
      ],
    ]);
  });

  it("shows chat and embedder rows with correct traits, type, and dimensions", async () => {
    stubApi({ "GET /api/models": () => json([verified, untestedEmbedder, verifiedEmbedder]) });
    renderPage("/providers");

    const chatRow = await findRow("fake:fake-tools");
    expect(within(chatRow).getByText("Chat")).toBeInTheDocument();
    expect(within(chatRow).getAllByText("Yes")).toHaveLength(2);
    expect(within(chatRow).getByText("32768")).toBeInTheDocument();
    expect(within(chatRow).getByText("Not applicable")).toBeInTheDocument(); // Dimensions

    const untestedRow = await findRow("fake:fake-embed");
    expect(within(untestedRow).getByText("Embedder")).toBeInTheDocument();
    expect(within(untestedRow).getAllByText("Not applicable")).toHaveLength(3); // Tool calling, Streaming, Context window
    expect(within(untestedRow).queryByRole("button", { name: "Set" })).not.toBeInTheDocument();
    expect(within(untestedRow).getByText("Not checked")).toBeInTheDocument(); // Dimensions

    const verifiedRow = await findRow("fake:verified-embed");
    expect(within(verifiedRow).getByText("Embedder")).toBeInTheDocument();
    expect(within(verifiedRow).getAllByText("Not applicable")).toHaveLength(3);
    expect(within(verifiedRow).queryByRole("button", { name: "Set" })).not.toBeInTheDocument();
    expect(within(verifiedRow).getByText("1536")).toBeInTheDocument(); // Dimensions
  });

  it("verifying an embedder updates its dimensions and availability", async () => {
    const sent = stubApi({
      "GET /api/models": () => json([untestedEmbedder]),
      "POST /api/models/verify": () =>
        json({
          ...untestedEmbedder,
          dimensions: 1536,
          availability: "verified",
          problem: null,
        }),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("fake:fake-embed");
    expect(within(row).getByText("Not checked")).toBeInTheDocument();
    expect(within(row).getAllByText("Not applicable")).toHaveLength(3);

    await user.click(within(row).getByRole("button", { name: "Verify fake:fake-embed" }));
    expect(await within(row).findByText("1536")).toBeInTheDocument();
    expect(within(row).getByText("Verified")).toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      ["POST", "/api/models/verify", { address: "fake:fake-embed" }],
    ]);
  });
});

describe("model prices", () => {
  const providerPrice = {
    prompt_per_mtok: 3,
    completion_per_mtok: 15,
    source: "provider",
  } as const;
  const userPrice = { prompt_per_mtok: 1.5, completion_per_mtok: 6, source: "user" } as const;

  it("shows a published price and where it came from", async () => {
    stubApi({ "GET /api/models": () => json([{ ...verified, price: providerPrice }]) });
    renderPage("/providers");

    const row = await findRow("fake:fake-tools");
    expect(within(row).getByText("$3.00 in / $15.00 out per 1M tokens")).toBeInTheDocument();
    expect(within(row).getByText("From the provider")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Clear price/ })).not.toBeInTheDocument();
  });

  it("says when a model has no price and sets one, sent for an address holding a slash", async () => {
    const priced = { ...slashed, price: userPrice };
    const sent = stubApi({
      "GET /api/models": () => json([slashed]),
      "PUT /api/models/price": () => json(priced),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("groq:openai/gpt-oss-120b");
    expect(within(row).getByText("No price")).toBeInTheDocument();
    await user.click(
      within(row).getByRole("button", { name: "Set price for groq:openai/gpt-oss-120b" }),
    );
    await user.type(await screen.findByLabelText("Prompt, USD per 1M tokens"), "1.5");
    await user.type(screen.getByLabelText("Completion, USD per 1M tokens"), "6");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await within(row).findByText("$1.50 in / $6.00 out per 1M tokens")).toBeInTheDocument();
    expect(within(row).getByText("Your price")).toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      [
        "PUT",
        "/api/models/price",
        {
          address: "groq:openai/gpt-oss-120b",
          price: { prompt_per_mtok: 1.5, completion_per_mtok: 6 },
        },
      ],
    ]);
  });

  it("shows the server's refusal under the field it names", async () => {
    stubApi({
      "GET /api/models": () => json([verified]),
      "PUT /api/models/price": () =>
        json(
          {
            code: "invalid_request",
            message: "Some fields are missing or invalid.",
            detail: {
              errors: [
                {
                  field: "body.price.prompt_per_mtok",
                  message: "Input should be greater than or equal to 0",
                },
              ],
            },
          },
          422,
        ),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("fake:fake-tools");
    await user.click(within(row).getByRole("button", { name: "Set price for fake:fake-tools" }));
    await user.type(await screen.findByLabelText("Prompt, USD per 1M tokens"), "-1");
    await user.type(screen.getByLabelText("Completion, USD per 1M tokens"), "1");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("Input should be greater than or equal to 0"),
    ).toBeInTheDocument();
  });

  it("shows any other refusal as it is", async () => {
    stubApi({
      "GET /api/models": () => json([verified]),
      "PUT /api/models/price": () =>
        json(
          { code: "model_not_found", message: "There is no model fake:fake-tools.", detail: {} },
          404,
        ),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("fake:fake-tools");
    await user.click(within(row).getByRole("button", { name: "Set price for fake:fake-tools" }));
    await user.type(await screen.findByLabelText("Prompt, USD per 1M tokens"), "1");
    await user.type(screen.getByLabelText("Completion, USD per 1M tokens"), "1");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("There is no model fake:fake-tools.")).toBeInTheDocument();
  });

  it("clears the user's price", async () => {
    const sent = stubApi({
      "GET /api/models": () => json([{ ...verified, price: userPrice }]),
      "PUT /api/models/price": () => json(verified),
    });
    const user = userEvent.setup();
    renderPage("/providers");

    const row = await findRow("fake:fake-tools");
    await user.click(within(row).getByRole("button", { name: "Clear price for fake:fake-tools" }));

    expect(await within(row).findByText("No price")).toBeInTheDocument();
    expect(writes(sent).map((s) => s.body)).toEqual([{ address: "fake:fake-tools", price: null }]);
  });

  it("has no price for an embedder", async () => {
    stubApi({ "GET /api/models": () => json([verifiedEmbedder]) });
    renderPage("/providers");

    const row = await findRow("fake:verified-embed");
    expect(within(row).queryByText("No price")).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /price/i })).not.toBeInTheDocument();
  });
});
