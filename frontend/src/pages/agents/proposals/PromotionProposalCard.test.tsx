import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { components } from "@/api/schema";
import { api } from "@/lib/api";
import type { ApprovalPart } from "../ApprovalCard";
import { PromotionProposalCard } from "./PromotionProposalCard";
import type { ProposalView } from "./useProposal";

type ReportView = components["schemas"]["ReportView"];

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
}

function renderWithClient(ui: ReactNode, queryClient = createTestQueryClient()) {
  return {
    queryClient,
    ...render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>),
  };
}

describe("PromotionProposalCard", () => {
  let postSpy: MockInstance;

  const baseApproval: ApprovalPart = {
    approval_id: "apr_promo_1",
    tool_call_id: "tc_promo_1",
    status: "pending",
    proposal_kind: "promotion",
    preview: {
      title: "Promote churn_predictor 2.0.0",
      kind: "text",
      body: "Fallback preview body text for promotion",
    },
    note: null,
    type: "approval",
    kind: "tool",
  };

  const sampleProposal: ProposalView = {
    id: "prp_promo_1",
    key: "churn_predictor",
    kind: "promotion",
    agent: "retraining",
    approval_id: "apr_promo_1",
    status: "pending",
    created: "2026-10-29T10:00:00Z",
    turn_id: "turn_promo_1",
    tool_call_id: "tc_promo_1",
    warnings: ["Approving runs the deploy hook."],
    slots: [],
    result: null,
    payload: {
      model: "churn_predictor",
      report_id: "rep_promo_1",
      candidate_version: "2.0.0",
      champion_version: "1.0.0",
      deploy_hook_id: "chk_deploy_churn",
    },
  };

  const sampleReport: ReportView = {
    id: "rep_promo_1",
    author: "agent_retraining",
    created: "2026-10-29T09:30:00Z",
    draft: {
      model: "churn_predictor",
      candidate_version: "2.0.0",
      fix_id: null,
      training_request_id: null,
      assessments: {
        fix_chosen: {
          value: "Retrained with 30-day extended window",
          not_applicable_reason: null,
        },
        leakage: {
          value: null,
          not_applicable_reason: "Feature store prevents temporal leakage",
        },
      },
    },
    evaluation: {
      record: {
        source_id: "src_mlflow",
        model: "churn_predictor",
        version: "2.0.0",
        run_key: "run_cand_2",
        schema_version: 1,
        payload: {},
        fetched_at: "2026-10-29T09:00:00Z",
      },
      payload: {
        schema_version: 1,
        evaluation_commit: "a1b2c3d",
        gate_config_version: "v2.1",
        champion_version: "1.0.0",
        decision: "promote",
        reasons: ["Loss decreased by 8%"],
        computed_where: "ci_pipeline_step_eval",
        champion_metrics: {
          overall: {
            roc_auc: {
              value: 0.85,
              ci_low: 0.83,
              ci_high: 0.87,
              n: 5000,
            },
          },
          per_group: {
            tier_enterprise: {
              roc_auc: {
                value: 0.88,
                ci_low: 0.84,
                ci_high: 0.92,
                n: 1200,
              },
            },
          },
        },
        challenger_metrics: {
          overall: {
            roc_auc: {
              value: 0.89,
              ci_low: 0.87,
              ci_high: 0.91,
              n: 5000,
            },
          },
          per_group: {
            tier_enterprise: {
              roc_auc: {
                value: 0.93,
                ci_low: 0.9,
                ci_high: 0.96,
                n: 1200,
              },
            },
          },
        },
      },
      not_computed: {
        serving_compatibility: "450",
        expected_cost: null,
      },
    },
  };

  beforeEach(() => {
    vi.spyOn(api, "GET").mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: sampleProposal,
          error: undefined,
          response: new Response(),
        });
      }
      if (url === "/api/projects/{project_id}/reports/{report_id}") {
        return Promise.resolve({
          data: sampleReport,
          error: undefined,
          response: new Response(),
        });
      }
      return Promise.reject(new Error("Unhandled GET"));
    });

    postSpy = vi.spyOn(api, "POST").mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/approvals/{approval_id}/decision") {
        return Promise.resolve({
          data: { status: "approved" },
          error: undefined,
          response: new Response(),
        });
      }
      return Promise.reject(new Error("Unhandled POST"));
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders report assessments, comparisons, computed mark, not-computed fields, and versions", async () => {
    renderWithClient(
      <PromotionProposalCard approval={baseApproval} projectId="prj_test" toolName="Propose" />,
    );

    // Wait for proposal and report to load
    await waitFor(() => {
      expect(screen.getByText("Retrained with 30-day extended window")).toBeInTheDocument();
    });

    // Check report assessment value and not_applicable_reason
    expect(screen.getByText("Retrained with 30-day extended window")).toBeInTheDocument();
    expect(screen.getByText(/Feature store prevents temporal leakage/)).toBeInTheDocument();

    // Check commit and gate version
    expect(screen.getByText(/a1b2c3d/)).toBeInTheDocument();
    expect(screen.getByText(/v2.1/)).toBeInTheDocument();

    // Check computed mark
    expect(screen.getByText(/computed by team step/i)).toBeInTheDocument();

    // Check not computed fields
    expect(screen.getByText(/serving_compatibility/i)).toBeInTheDocument();
    expect(screen.getByText(/PR #450/)).toBeInTheDocument();

    // Check champion vs challenger metrics (overall and per-group)
    expect(screen.getAllByText(/roc_auc/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/0.85/)).toBeInTheDocument();
    expect(screen.getByText(/0.89/)).toBeInTheDocument();
    expect(screen.getByText(/tier_enterprise/i)).toBeInTheDocument();
    expect(screen.getByText(/0.88/)).toBeInTheDocument();
    expect(screen.getByText(/0.93/)).toBeInTheDocument();
  });

  it("Approve button sends decision request with approve: true", async () => {
    const user = userEvent.setup();
    renderWithClient(
      <PromotionProposalCard approval={baseApproval} projectId="prj_test" toolName="Propose" />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith(
        "/api/projects/{project_id}/approvals/{approval_id}/decision",
        {
          params: {
            path: {
              project_id: "prj_test",
              approval_id: "apr_promo_1",
            },
          },
          body: {
            approve: true,
            note: null,
          },
        },
      );
    });
  });

  it("Reject button sends decision request with approve: false", async () => {
    const user = userEvent.setup();
    renderWithClient(
      <PromotionProposalCard approval={baseApproval} projectId="prj_test" toolName="Propose" />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith(
        "/api/projects/{project_id}/approvals/{approval_id}/decision",
        {
          params: {
            path: {
              project_id: "prj_test",
              approval_id: "apr_promo_1",
            },
          },
          body: {
            approve: false,
            note: null,
          },
        },
      );
    });
  });
});
