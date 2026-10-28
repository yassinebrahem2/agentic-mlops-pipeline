import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { components } from "@/api/schema";
import { ProviderCard } from "./ProviderCard";

type Provider = components["schemas"]["Provider"];

function renderProviderCard(provider: Provider) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProviderCard provider={provider} />
    </QueryClientProvider>,
  );
}

const groqProvider: Provider = {
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

const emptyHostedProvider: Provider = {
  id: "prv_2",
  name: "empty-hosted",
  kind: "hosted",
  base_url: "https://api.example.com/v1",
  status: "ok",
  status_detail: null,
  cooldown_minutes: 15,
  keys: [],
};

describe("ProviderCard", () => {
  it("renders with compact size sm", () => {
    const { container } = renderProviderCard(groqProvider);
    const card = container.querySelector('[data-slot="card"]');
    expect(card).toBeInTheDocument();
    expect(card).toHaveAttribute("data-size", "sm");
  });

  it("places Remove provider in CardAction slot in CardHeader", () => {
    const { container } = renderProviderCard(groqProvider);
    const header = container.querySelector('[data-slot="card-header"]');
    expect(header).toBeInTheDocument();

    const action = header?.querySelector('[data-slot="card-action"]');
    expect(action).toBeInTheDocument();
    expect(
      within(action as HTMLElement).getByRole("button", { name: "Remove provider groq" }),
    ).toBeInTheDocument();
  });

  it("renders base URL in CardDescription with text-muted-foreground break-all", () => {
    const { container } = renderProviderCard(groqProvider);
    const desc = container.querySelector('[data-slot="card-description"]');
    expect(desc).toBeInTheDocument();
    expect(desc).toHaveTextContent("https://api.groq.com/openai/v1");
    expect(desc).toHaveClass("text-muted-foreground", "break-all");
  });

  it("shows empty key message and Add key button on the same row when keys are empty", () => {
    renderProviderCard(emptyHostedProvider);
    const emptyMsg = screen.getByText("No keys yet.");
    expect(emptyMsg).toBeInTheDocument();

    const row = emptyMsg.parentElement;
    expect(row).toHaveClass("flex", "items-center", "justify-between");
    expect(within(row as HTMLElement).getByRole("button", { name: "Add key" })).toBeInTheDocument();
  });

  it("renders Add key aligned at end after key list when keys exist", () => {
    renderProviderCard(groqProvider);
    expect(screen.getByText("gsk_...abcd")).toBeInTheDocument();
    expect(screen.getByText("gsk_...wxyz")).toBeInTheDocument();

    const addKeyBtn = screen.getByRole("button", { name: "Add key" });
    expect(addKeyBtn).toBeInTheDocument();
    expect(addKeyBtn.parentElement).toHaveClass("flex", "justify-end");
  });

  it("allows opening and canceling the Add key form", async () => {
    const user = userEvent.setup();
    renderProviderCard(groqProvider);

    await user.click(screen.getByRole("button", { name: "Add key" }));
    expect(screen.getByLabelText("New key for groq")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("New key for groq")).not.toBeInTheDocument();
  });
});
