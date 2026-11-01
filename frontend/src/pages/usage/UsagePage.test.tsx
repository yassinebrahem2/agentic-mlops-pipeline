import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { project } from "@/test/projects";
import { renderPage } from "@/test/render";

type UsageView = components["schemas"]["UsageView"];
type UsageRow = components["schemas"]["UsageRow"];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(usage: () => Response | Promise<Response>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const path = new URL(input.url).pathname;
      if (path === "/api/usage") return usage();
      if (path === "/api/projects") return json([project("prj_1", "Churn model", null)]);
      throw new Error(`Unexpected ${input.method} ${path}`);
    }),
  );
}

const priced: UsageRow = {
  project_id: "prj_1",
  agent: "orchestrator",
  model_address: "groq:llama",
  week_start: "2026-10-26",
  prompt_tokens: 1200,
  completion_tokens: 300,
  cost_usd: 0.75,
};
const pricedOlder: UsageRow = { ...priced, week_start: "2026-10-19", cost_usd: 0.25 };
const unpriced: UsageRow = {
  ...priced,
  agent: "auditor",
  model_address: "fake:free",
  prompt_tokens: 50,
  completion_tokens: 5,
  cost_usd: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("usage page", () => {
  it("lists tokens and cost per project, agent, model and week, with totals", async () => {
    const view: UsageView = { rows: [priced, pricedOlder], unpriced_models: [] };
    stubApi(() => json(view));
    renderPage("/usage");

    const rows = await screen.findAllByRole("row");
    const first = rows.find((r) => within(r).queryByText("2026-10-26"));
    if (!first) throw new Error("no row for the first week");
    expect(within(first).getByText("Churn model")).toBeInTheDocument();
    expect(within(first).getByText("orchestrator")).toBeInTheDocument();
    expect(within(first).getByText("groq:llama")).toBeInTheDocument();
    expect(within(first).getByText("1,200")).toBeInTheDocument();
    expect(within(first).getByText("300")).toBeInTheDocument();
    expect(within(first).getByText("$0.75")).toBeInTheDocument();

    expect(screen.getByText("Total cost").nextElementSibling).toHaveTextContent("$1.00");
    expect(screen.getByText("Total tokens").nextElementSibling).toHaveTextContent("3,000");
    expect(screen.queryByText(/partial/i)).not.toBeInTheDocument();
  });

  it("calls the total partial and names the unpriced models when some have no price", async () => {
    stubApi(() => json({ rows: [priced, unpriced], unpriced_models: ["fake:free"] }));
    renderPage("/usage");

    expect(await screen.findByText(/partial/i)).toBeInTheDocument();
    expect(screen.getByText("Total cost").nextElementSibling).toHaveTextContent("$0.75");
    expect(screen.getByText(/No price set for:/)).toHaveTextContent("fake:free");
    const row = screen.getByText("auditor").closest("tr");
    if (!row) throw new Error("no row for the unpriced call");
    expect(within(row).getByText("–")).toBeInTheDocument();
    expect(within(row).getByText("50")).toBeInTheDocument();
    expect(within(row).getByText("5")).toBeInTheDocument();
  });

  it("shows tokens only when no model has a price", async () => {
    stubApi(() => json({ rows: [unpriced], unpriced_models: ["fake:free"] }));
    renderPage("/usage");

    expect(await screen.findByText(/tokens only/i)).toBeInTheDocument();
    expect(screen.queryByText("Total cost")).not.toBeInTheDocument();
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
    expect(screen.getByText("Total tokens").nextElementSibling).toHaveTextContent("55");
  });

  it("says so when no model call has been recorded", async () => {
    stubApi(() => json({ rows: [], unpriced_models: [] }));
    renderPage("/usage");
    expect(await screen.findByText(/No model calls yet/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows a loading state and then the server's error", async () => {
    stubApi(() => json({ code: "boom", message: "Usage is unavailable.", detail: {} }, 500));
    renderPage("/usage");

    expect(screen.getByText("Loading usage…")).toBeInTheDocument();
    expect(await screen.findByText("Usage is unavailable.")).toBeInTheDocument();
  });

  it("is reachable from the app navigation", async () => {
    stubApi(() => json({ rows: [], unpriced_models: [] }));
    renderPage("/providers");
    expect(await screen.findByRole("link", { name: "Usage" })).toHaveAttribute("href", "/usage");
  });
});
