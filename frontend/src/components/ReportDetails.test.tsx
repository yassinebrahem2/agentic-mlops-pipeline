import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "@/api/schema";
import { ReportDetails } from "./ReportDetails";

type ReportView = components["schemas"]["ReportView"];

const report: ReportView = {
  id: "rep_1",
  author: "agent_retraining",
  created: "2026-10-29T09:30:00Z",
  draft: {
    model: "churn",
    candidate_version: "2",
    fix_id: null,
    training_request_id: null,
    assessments: {
      fix_chosen: { value: "Longer window", not_applicable_reason: null },
      leakage: { value: null, not_applicable_reason: "Feature store prevents it" },
    },
  },
  evaluation: {
    record: {
      source_id: "src",
      model: "churn",
      version: "2",
      run_key: "run",
      schema_version: 1,
      payload: {},
      fetched_at: "2026-10-29T09:00:00Z",
    },
    payload: {
      schema_version: 1,
      evaluation_commit: "a1b2c3d",
      gate_config_version: "v2.1",
      champion_version: "1",
      decision: "promote",
      reasons: ["Loss decreased"],
      computed_where: "ci_step",
      champion_metrics: { overall: { auc: { value: 0.85, n: 100 } }, per_group: {} },
      challenger_metrics: { overall: { auc: { value: 0.9, n: 100 } }, per_group: {} },
    },
    not_computed: { serving_compatibility: "450", expected_cost: null },
  },
};

describe("ReportDetails", () => {
  it("marks the evaluation as computed and shows commit, decision and metrics", () => {
    render(<ReportDetails report={report} />);
    expect(screen.getByText("computed by team step (ci_step)")).toBeInTheDocument();
    expect(screen.getByText("a1b2c3d")).toBeInTheDocument();
    expect(screen.getByText("promote")).toBeInTheDocument();
    expect(screen.getByText("0.85 (n=100)")).toBeInTheDocument();
    expect(screen.getByText("0.9 (n=100)")).toBeInTheDocument();
  });

  it("lists fields that were not computed, with the PR that owns them", () => {
    render(<ReportDetails report={report} />);
    const list = screen.getByText("Not Computed:").parentElement as HTMLElement;
    expect(within(list).getByText("serving_compatibility")).toBeInTheDocument();
    expect(within(list).getByText("PR #450")).toBeInTheDocument();
    expect(within(list).getByText("None")).toBeInTheDocument();
  });

  it("shows an assessment's value or why it is not applicable", () => {
    render(<ReportDetails report={report} />);
    expect(screen.getByText("Longer window")).toBeInTheDocument();
    expect(screen.getByText("Not applicable: Feature store prevents it")).toBeInTheDocument();
  });

  it("omits the evaluation block when the report has no evaluation", () => {
    render(<ReportDetails report={{ ...report, evaluation: null }} />);
    expect(screen.queryByText("Evaluation")).toBeNull();
    expect(screen.getByText("Report Assessments")).toBeInTheDocument();
  });
});
