import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { formatTime } from "@/lib/format";
import { TeamAlerts } from "./TeamAlerts";

describe("TeamAlerts", () => {
  it("renders null when alerts is undefined, null, or empty", () => {
    const { container: c1 } = render(<TeamAlerts alerts={undefined} />);
    expect(c1.firstChild).toBeNull();

    const { container: c2 } = render(<TeamAlerts alerts={null} />);
    expect(c2.firstChild).toBeNull();

    const { container: c3 } = render(<TeamAlerts alerts={[]} />);
    expect(c3.firstChild).toBeNull();
  });

  it("renders alerts with check_id, subject, formatted ran_at, and verdict", () => {
    render(
      <TeamAlerts
        alerts={[
          {
            result_id: "res_1",
            check_id: "psi_age",
            subject: "drift on features",
            category: "drift",
            ran_at: "2026-11-03T10:00:00Z",
            verdict: "real_issue",
          },
          {
            result_id: "res_2",
            check_id: "null_rate",
            subject: "missing values check",
            category: "data_quality",
            ran_at: "2026-11-03T09:00:00Z",
            verdict: "not_an_issue",
          },
          {
            result_id: "res_3",
            check_id: "auc_floor",
            subject: "model performance check",
            category: "model_performance",
            ran_at: "2026-11-03T08:00:00Z",
            verdict: null,
          },
        ]}
      />,
    );

    expect(screen.getByText("Your team's alerts")).toBeInTheDocument();

    const alert1 = screen.getByTestId("team-alert-res_1");
    expect(within(alert1).getByText("psi_age")).toBeInTheDocument();
    expect(within(alert1).getByText("drift on features")).toBeInTheDocument();
    expect(within(alert1).getByText(formatTime("2026-11-03T10:00:00Z"))).toBeInTheDocument();
    const verdict1 = within(alert1).getByText("Real issue");
    expect(verdict1).toHaveAttribute("data-variant", "destructive");

    const alert2 = screen.getByTestId("team-alert-res_2");
    expect(within(alert2).getByText("null_rate")).toBeInTheDocument();
    expect(within(alert2).getByText("missing values check")).toBeInTheDocument();
    expect(within(alert2).getByText(formatTime("2026-11-03T09:00:00Z"))).toBeInTheDocument();
    const verdict2 = within(alert2).getByText("Not an issue");
    expect(verdict2).toHaveAttribute("data-variant", "secondary");

    const alert3 = screen.getByTestId("team-alert-res_3");
    expect(within(alert3).getByText("auc_floor")).toBeInTheDocument();
    expect(within(alert3).getByText("model performance check")).toBeInTheDocument();
    expect(within(alert3).getByText(formatTime("2026-11-03T08:00:00Z"))).toBeInTheDocument();
    const verdict3 = within(alert3).getByText("None");
    expect(verdict3).toHaveAttribute("data-variant", "outline");
  });

  it("sorts alerts newest first by ran_at", () => {
    render(
      <TeamAlerts
        alerts={[
          {
            result_id: "older",
            check_id: "check_older",
            subject: "older alert",
            category: "drift",
            ran_at: "2026-11-01T10:00:00Z",
            verdict: null,
          },
          {
            result_id: "newer",
            check_id: "check_newer",
            subject: "newer alert",
            category: "drift",
            ran_at: "2026-11-03T10:00:00Z",
            verdict: null,
          },
        ]}
      />,
    );

    const items = screen.getAllByTestId(/^team-alert-/);
    expect(items[0]).toHaveAttribute("data-testid", "team-alert-newer");
    expect(items[1]).toHaveAttribute("data-testid", "team-alert-older");
  });
});
