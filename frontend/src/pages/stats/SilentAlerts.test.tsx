import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { formatTime } from "@/lib/format";
import { renderPage } from "@/test/render";
import { json, sampleAlerts, STATS_PATH, stubApi } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function alertRow(checkId: string): Promise<HTMLElement> {
  const cell = await screen.findByText(checkId);
  const row = cell.closest("tr");
  if (!row) throw new Error(`no row for ${checkId}`);
  return row;
}

describe("silent team alerts", () => {
  it("lists each alert's check, subject, time and verdict", async () => {
    stubApi({ silent: () => json(sampleAlerts) });
    renderPage(STATS_PATH);

    const first = await alertRow("team_null_rate");
    expect(within(first).getByText("orders.amount")).toBeInTheDocument();
    expect(within(first).getByText(formatTime("2026-10-30T09:00:00Z"))).toBeInTheDocument();
    expect(within(first).getByText("not judged")).toBeInTheDocument();
    expect(within(first).getByRole("button", { name: "Real issue" })).toBeEnabled();
    expect(within(first).getByRole("button", { name: "Not an issue" })).toBeEnabled();

    const second = await alertRow("team_latency");
    expect(within(second).getByText("scoring-api")).toBeInTheDocument();
    expect(within(second).getByText("real issue")).toBeInTheDocument();
  });

  it("says so when no team alert went unanswered", async () => {
    stubApi({ silent: () => json([]) });
    renderPage(STATS_PATH);
    expect(await screen.findByText("No silent team alerts.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Real issue" })).not.toBeInTheDocument();
  });

  it("posts the verdict, disables the buttons meanwhile, then refetches the list and the stats", async () => {
    const user = userEvent.setup();
    let answer: (r: Response) => void = () => undefined;
    const calls = stubApi({
      silent: () => json(sampleAlerts),
      verdict: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    renderPage(STATS_PATH);

    const row = await alertRow("team_null_rate");
    await screen.findByRole("group", { name: "Precision" });
    const statsBefore = calls.stats.length;
    const silentBefore = calls.silent;

    await user.click(within(row).getByRole("button", { name: "Not an issue" }));

    await waitFor(() => {
      expect(calls.verdicts).toEqual([{ resultId: "res_1", body: '{"value":"not_an_issue"}' }]);
    });
    for (const button of screen.getAllByRole("button", { name: /^(Real issue|Not an issue)$/ })) {
      expect(button).toBeDisabled();
    }

    answer(json({ ...sampleAlerts[0], verdict: "not_an_issue" }));
    await waitFor(() => {
      expect(calls.silent).toBeGreaterThan(silentBefore);
      expect(calls.stats.length).toBeGreaterThan(statsBefore);
    });
  });

  it("posts real_issue for the other button", async () => {
    const user = userEvent.setup();
    const calls = stubApi({
      silent: () => json(sampleAlerts),
      verdict: () => json({ ...sampleAlerts[1], verdict: "real_issue" }),
    });
    renderPage(STATS_PATH);

    const row = await alertRow("team_latency");
    await user.click(within(row).getByRole("button", { name: "Real issue" }));
    await waitFor(() => {
      expect(calls.verdicts).toEqual([{ resultId: "res_2", body: '{"value":"real_issue"}' }]);
    });
  });

  it("shows the server's error when a verdict fails", async () => {
    const user = userEvent.setup();
    stubApi({
      silent: () => json(sampleAlerts),
      verdict: () =>
        json({ code: "not_found", message: "That alert is no longer silent.", detail: {} }, 404),
    });
    renderPage(STATS_PATH);

    const row = await alertRow("team_null_rate");
    await user.click(within(row).getByRole("button", { name: "Real issue" }));
    expect(await screen.findByText("That alert is no longer silent.")).toBeInTheDocument();
  });

  it("shows a loading state and then the server's error for the list", async () => {
    stubApi({
      silent: () => json({ code: "boom", message: "Alerts are unavailable.", detail: {} }, 500),
    });
    renderPage(STATS_PATH);

    expect(await screen.findByText("Loading silent team alerts…")).toBeInTheDocument();
    expect(await screen.findByText("Alerts are unavailable.")).toBeInTheDocument();
  });
});
