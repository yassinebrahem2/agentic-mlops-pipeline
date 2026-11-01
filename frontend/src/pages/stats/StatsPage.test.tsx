import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderPage } from "@/test/render";
import { groupLabel, metricLabel } from "./metricLabels";
import { json, sampleStats, STATS_PATH, stubApi } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function tile(label: string): Promise<HTMLElement> {
  return screen.findByRole("group", { name: label });
}

describe("stats page", () => {
  it("renders every group and metric of the response", async () => {
    stubApi({});
    renderPage(STATS_PATH);

    for (const [group, metrics] of Object.entries(sampleStats.groups)) {
      const section = await screen.findByRole("region", { name: groupLabel(group) });
      for (const name of Object.keys(metrics)) {
        expect(
          within(section).getByRole("group", { name: metricLabel(name).label }),
        ).toBeInTheDocument();
      }
    }
    // Names the page does not know show by their raw name.
    expect(screen.getByRole("region", { name: "future_group" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "brand_new_metric" })).toBeInTheDocument();
  });

  it("shows each value with its unit and what it rests on", async () => {
    stubApi({});
    renderPage(STATS_PATH);

    const precision = await tile("Precision");
    expect(within(precision).getByText("80%")).toBeInTheDocument();
    expect(within(precision).getByText("based on 10")).toBeInTheDocument();

    expect(within(await tile("Detection delay")).getByText("1 min 30 s")).toBeInTheDocument();
    expect(within(await tile("Cost per incident")).getByText("$1.50")).toBeInTheDocument();
    expect(within(await tile("Misses")).getByText("3")).toBeInTheDocument();
    // An average token count is still a whole number of tokens.
    expect(within(await tile("Tokens per agent")).getByText("1,235")).toBeInTheDocument();
    expect(within(await tile("brand_new_metric")).getByText("7.00")).toBeInTheDocument();

    // Few incidents but not collecting: the API decides, so the value shows.
    const alerts = await tile("Alerts per week");
    expect(within(alerts).getByText("2.50")).toBeInTheDocument();
    expect(within(alerts).getByText("based on 2")).toBeInTheDocument();
  });

  it("shows collecting data instead of the value when the API says collecting", async () => {
    stubApi({});
    renderPage(STATS_PATH);

    const caught = await tile("Caught, team missed");
    expect(within(caught).getByText("collecting data (based on 12)")).toBeInTheDocument();
    expect(within(caught).queryByText("2")).not.toBeInTheDocument();
    expect(within(caught).queryByText("2.00")).not.toBeInTheDocument();
  });

  it("shows not available for a missing value, never 0", async () => {
    stubApi({});
    renderPage(STATS_PATH);

    const failed = await tile("Failed runs");
    expect(within(failed).getByText("not available")).toBeInTheDocument();
    expect(within(failed).queryByText("0")).not.toBeInTheDocument();
  });

  it("shows collecting data, not not available, when there are no verdicts yet", async () => {
    stubApi({});
    renderPage(STATS_PATH);

    const resolution = await tile("Time to resolution");
    expect(within(resolution).getByText("collecting data (based on 0)")).toBeInTheDocument();
    expect(within(resolution).queryByText("not available")).not.toBeInTheDocument();
  });

  it("says the team's monitoring is not readable yet when the baseline is unavailable", async () => {
    stubApi({
      stats: () =>
        json({
          ...sampleStats,
          baseline_available: false,
          groups: {
            detection: {
              precision: { value: 0.8, n: 10, collecting: false },
              caught_baseline_missed: { value: null, n: 10, collecting: false },
            },
          },
        }),
    });
    renderPage(STATS_PATH);

    expect(
      await screen.findByText(/team's own monitoring is not readable yet/),
    ).toBeInTheDocument();
    const caught = await tile("Caught, team missed");
    expect(within(caught).getByText("not available")).toBeInTheDocument();
  });

  it("shows no baseline notice when the baseline is available", async () => {
    stubApi({});
    renderPage(STATS_PATH);

    await tile("Precision");
    expect(screen.queryByText(/team's own monitoring is not readable yet/)).not.toBeInTheDocument();
  });

  it("refetches with the chosen window and category", async () => {
    const user = userEvent.setup();
    const calls = stubApi({});
    renderPage(STATS_PATH);

    await tile("Precision");
    const first = calls.stats[0];
    expect(first?.searchParams.get("window")).toBe("30d");
    expect(first?.searchParams.has("category")).toBe(false);

    const windows = screen.getByRole("tablist", { name: "Time window" });
    await user.click(within(windows).getByRole("tab", { name: "7 days" }));
    await waitFor(() => {
      expect(calls.stats.at(-1)?.searchParams.get("window")).toBe("7d");
    });

    const categories = screen.getByRole("tablist", { name: "Category" });
    await user.click(within(categories).getByRole("tab", { name: "Drift" }));
    await waitFor(() => {
      expect(calls.stats.at(-1)?.searchParams.get("category")).toBe("drift");
    });
    expect(calls.stats.at(-1)?.searchParams.get("window")).toBe("7d");
  });

  it("shows a loading state and then the server's error", async () => {
    stubApi({
      stats: () => json({ code: "boom", message: "Stats are unavailable.", detail: {} }, 500),
    });
    renderPage(STATS_PATH);

    expect(await screen.findByText("Loading stats…")).toBeInTheDocument();
    expect(await screen.findByText("Stats are unavailable.")).toBeInTheDocument();
  });

  it("is reachable from the project navigation", async () => {
    stubApi({});
    renderPage(STATS_PATH);
    expect(await screen.findByRole("link", { name: "Stats" })).toHaveAttribute("href", STATS_PATH);
  });
});
