import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AtRiskList } from "./AtRiskList";

describe("AtRiskList", () => {
  it("renders not_judged expectation with Not judged yet text and secondary variant", () => {
    render(
      <AtRiskList
        items={[
          {
            key: { kind: "check", ref: "psi_drift", model: "churn" },
            category: "drift",
            active: true,
            status: "not_judged",
            next_deadline: null,
            last_seen: null,
            since: null,
          },
        ]}
      />,
    );

    const item = screen.getByTestId("at-risk-check-psi_drift");
    expect(within(item).getByText("psi_drift")).toBeInTheDocument();
    expect(within(item).getByText("check of churn")).toBeInTheDocument();
    const badge = within(item).getByText("Not judged yet");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute("data-variant", "secondary");
  });

  it("renders due status with outline badge", () => {
    render(
      <AtRiskList
        items={[
          {
            key: { kind: "feed", ref: "daily_scores", model: null },
            category: "data_quality",
            active: true,
            status: "due",
            next_deadline: "2026-11-01T06:00:00Z",
            last_seen: null,
            since: null,
          },
        ]}
      />,
    );

    const item = screen.getByTestId("at-risk-feed-daily_scores");
    const badge = within(item).getByText("due");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute("data-variant", "outline");
  });

  it("renders late and silent statuses with destructive badge", () => {
    render(
      <AtRiskList
        items={[
          {
            key: { kind: "job", ref: "nightly_eval", model: null },
            category: "model_performance",
            active: true,
            status: "late",
            next_deadline: null,
            last_seen: null,
            since: null,
          },
          {
            key: { kind: "feed", ref: "bookings", model: null },
            category: "data_quality",
            active: true,
            status: "silent",
            next_deadline: null,
            last_seen: null,
            since: null,
          },
        ]}
      />,
    );

    const lateBadge = within(screen.getByTestId("at-risk-job-nightly_eval")).getByText("late");
    expect(lateBadge).toHaveAttribute("data-variant", "destructive");
    const silentBadge = within(screen.getByTestId("at-risk-feed-bookings")).getByText("silent");
    expect(silentBadge).toHaveAttribute("data-variant", "destructive");
  });

  it("renders empty state when there are no items", () => {
    render(<AtRiskList items={[]} />);
    expect(screen.getByText("Nothing is due, late or silent.")).toBeInTheDocument();
  });
});
