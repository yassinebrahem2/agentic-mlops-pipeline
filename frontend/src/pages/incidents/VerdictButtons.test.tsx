import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VerdictButtons } from "./VerdictButtons";
import { bodyOf, failure, ok, postCount, stubPost, VERDICT } from "./testing";

function renderButtons() {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <VerdictButtons projectId="prj_1" incidentId="inc_1" />
    </QueryClientProvider>,
  );
}

describe("VerdictButtons", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("posts a real issue verdict without a category", async () => {
    const user = userEvent.setup();
    const post = stubPost({ [VERDICT]: () => ok({}) });
    renderButtons();

    await user.click(screen.getByRole("button", { name: "Real issue" }));

    await waitFor(() => {
      expect(bodyOf(post, VERDICT)).toEqual({ value: "real_issue" });
    });
  });

  it("posts not an issue with the note, and omits an empty note", async () => {
    const user = userEvent.setup();
    const post = stubPost({ [VERDICT]: () => ok({}) });
    renderButtons();

    await user.type(screen.getByLabelText("Note (optional)"), "  planned backfill ");
    await user.click(screen.getByRole("button", { name: "Not an issue" }));

    await waitFor(() => {
      expect(bodyOf(post, VERDICT)).toEqual({ value: "not_an_issue", note: "planned backfill" });
    });
  });

  it("requires a category for a wrong type and sends the optional subtype", async () => {
    const user = userEvent.setup();
    const post = stubPost({ [VERDICT]: () => ok({}) });
    renderButtons();

    await user.click(screen.getByRole("button", { name: "Wrong type" }));
    const submit = screen.getByRole("button", { name: "Record wrong type" });
    expect(submit).toBeDisabled();
    await user.click(submit);
    expect(postCount(post, VERDICT)).toBe(0);

    await user.click(screen.getByRole("combobox", { name: "Right category" }));
    await user.click(await screen.findByRole("option", { name: "Data quality" }));
    await user.type(screen.getByLabelText("Right subtype (optional)"), "schema_change");
    await user.click(screen.getByRole("button", { name: "Record wrong type" }));

    await waitFor(() => {
      expect(bodyOf(post, VERDICT)).toEqual({
        value: "wrong_type",
        right_category: "data_quality",
        right_subtype: "schema_change",
      });
    });
  });

  it("shows the server's refusal as is and keeps the buttons usable", async () => {
    const user = userEvent.setup();
    stubPost({
      [VERDICT]: () => failure("incident_not_found", "Incident 'inc_1' not found.", 404),
    });
    renderButtons();

    await user.click(screen.getByRole("button", { name: "Real issue" }));

    expect(await screen.findByText("Incident 'inc_1' not found.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Real issue" })).toBeEnabled();
  });

  it("disables every button while a verdict is pending", async () => {
    const user = userEvent.setup();
    stubPost({ [VERDICT]: () => new Promise(() => undefined) });
    renderButtons();

    await user.click(screen.getByRole("button", { name: "Not an issue" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Real issue" })).toBeDisabled();
    });
    expect(screen.getByRole("button", { name: "Not an issue" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Wrong type" })).toBeDisabled();
  });
});
