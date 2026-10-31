import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MissReportDialog } from "./MissReportDialog";
import { bodyOf, failure, MISSES, ok, stubPost } from "./testing";

function renderDialog() {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MissReportDialog projectId="prj_1" />
    </QueryClientProvider>,
  );
}

async function fill(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Report a miss" }));
  await user.type(await screen.findByLabelText("What happened"), "Nulls in the age column");
  await user.type(screen.getByLabelText("When it happened"), "2026-10-29T14:30");
}

describe("MissReportDialog", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps the submit disabled until a description and a time are given", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("button", { name: "Report a miss" }));
    expect(await screen.findByRole("button", { name: "Send report" })).toBeDisabled();
  });

  it("posts the report with the chosen category and closes", async () => {
    const user = userEvent.setup();
    const post = stubPost({ [MISSES]: () => ok(undefined, 204) });
    renderDialog();
    await fill(user);
    await user.click(screen.getByRole("combobox", { name: "Category (optional)" }));
    await user.click(await screen.findByRole("option", { name: "Drift" }));

    await user.click(screen.getByRole("button", { name: "Send report" }));

    await waitFor(() => {
      expect(bodyOf(post, MISSES)).toEqual({
        description: "Nulls in the age column",
        category: "drift",
        occurred_at: new Date("2026-10-29T14:30").toISOString(),
      });
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("omits the category when none is chosen", async () => {
    const user = userEvent.setup();
    const post = stubPost({ [MISSES]: () => ok(undefined, 204) });
    renderDialog();
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Send report" }));
    await waitFor(() => {
      expect(bodyOf(post, MISSES)).toEqual({
        description: "Nulls in the age column",
        occurred_at: new Date("2026-10-29T14:30").toISOString(),
      });
    });
  });

  it("shows the server's message and stays open when the report is refused", async () => {
    const user = userEvent.setup();
    stubPost({ [MISSES]: () => failure("invalid_request", "description is too long", 422) });
    renderDialog();
    await fill(user);

    await user.click(screen.getByRole("button", { name: "Send report" }));

    expect(await screen.findByText("description is too long")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
