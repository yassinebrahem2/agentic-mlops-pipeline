import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "../ApprovalCard";
import { TrainingPolicyProposalCard } from "./TrainingPolicyProposalCard";
import type { ProposalView } from "./useProposal";

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

describe("TrainingPolicyProposalCard", () => {
  let getSpy: MockInstance;

  const baseApproval: ApprovalPart = {
    approval_id: "apr_pol_1",
    tool_call_id: "tc_1",
    status: "pending",
    proposal_kind: "training_policy",
    preview: {
      title: "Proposal: training_policy",
      kind: "text",
      body: "Fallback preview body text for training policy",
    },
    note: null,
    type: "approval",
    kind: "tool",
  };

  const sampleProposal: ProposalView = {
    id: "prop_1",
    key: "churn_risk",
    kind: "training_policy",
    agent: "retraining",
    approval_id: "apr_pol_1",
    status: "pending",
    created: "2026-10-31T10:00:00Z",
    turn_id: "turn_1",
    tool_call_id: "tc_1",
    warnings: [
      "The cited hook deploys to production.",
      "The run-time limit is the team's to enforce.",
    ],
    slots: [],
    result: null,
    payload: {
      model: "churn_risk",
      train_hook_id: "hok_train_1",
      evaluate_hook_id: "hok_eval_1",
      window: {
        kind: "trailing_days",
        days: 30,
        start: null,
        end: null,
      },
      min_interval_hours: 24,
      max_runs_per_week: 3,
      run_time_limit_minutes: 120,
    },
  };

  beforeEach(() => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (
        path === "/api/projects/{project_id}/proposals/by-approval/{approval_id}" ||
        path === "/api/projects/{project_id}/proposals/{proposal_id}"
      ) {
        return Promise.resolve({
          data: sampleProposal,
          error: undefined,
          response: new Response(),
        });
      }
      return Promise.resolve({ data: sampleProposal, error: undefined, response: new Response() });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders fallback preview while proposal is loading", () => {
    getSpy.mockImplementation(() => new Promise(() => {}));
    renderWithClient(<TrainingPolicyProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(screen.getByText("Fallback preview body text for training policy")).toBeInTheDocument();
  });

  it("renders model, hooks, window rule, limits, and warnings when proposal loads", async () => {
    renderWithClient(<TrainingPolicyProposalCard approval={baseApproval} projectId="prj_1" />);

    await waitFor(() => {
      expect(screen.getByText("churn_risk")).toBeInTheDocument();
    });

    expect(screen.getByText("hok_train_1")).toBeInTheDocument();
    expect(screen.getByText("hok_eval_1")).toBeInTheDocument();
    expect(screen.getByText(/Trailing days:/)).toBeInTheDocument();
    expect(screen.getByText("30")).toBeInTheDocument();
    expect(screen.getByText("24")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("120")).toBeInTheDocument();

    // Warnings rendered by ProposalShell
    expect(screen.getByText("The cited hook deploys to production.")).toBeInTheDocument();
    expect(screen.getByText("The run-time limit is the team's to enforce.")).toBeInTheDocument();
  });

  it("renders since_last_run window rule properly", async () => {
    const sinceLastRunProposal: ProposalView = {
      ...sampleProposal,
      payload: {
        ...sampleProposal.payload,
        window: {
          kind: "since_last_run",
          days: null,
          start: null,
          end: null,
        },
      },
    };
    getSpy.mockImplementation(() =>
      Promise.resolve({
        data: sinceLastRunProposal,
        error: undefined,
        response: new Response(),
      }),
    );

    renderWithClient(<TrainingPolicyProposalCard approval={baseApproval} projectId="prj_1" />);

    await waitFor(() => {
      expect(screen.getByText("Since last run")).toBeInTheDocument();
    });
  });

  it("renders fixed window rule properly", async () => {
    const fixedProposal: ProposalView = {
      ...sampleProposal,
      payload: {
        ...sampleProposal.payload,
        window: {
          kind: "fixed",
          days: null,
          start: "2026-01-01T00:00:00Z",
          end: "2026-06-01T00:00:00Z",
        },
      },
    };
    getSpy.mockImplementation(() =>
      Promise.resolve({
        data: fixedProposal,
        error: undefined,
        response: new Response(),
      }),
    );

    renderWithClient(<TrainingPolicyProposalCard approval={baseApproval} projectId="prj_1" />);

    await waitFor(() => {
      expect(screen.getByText(/2026-01-01T00:00:00Z/)).toBeInTheDocument();
      expect(screen.getByText(/2026-06-01T00:00:00Z/)).toBeInTheDocument();
    });
  });

  it("dispatches from ApprovalCard for proposal_kind training_policy", async () => {
    renderWithClient(<ApprovalCard approval={baseApproval} projectId="prj_1" />);

    await waitFor(() => {
      expect(screen.getByText("churn_risk")).toBeInTheDocument();
    });
  });

  it("renders applied and failed results when proposal is settled", async () => {
    const settledProposal: ProposalView = {
      ...sampleProposal,
      approval_id: "apr_pol_settled",
      status: "approved",
      result: {
        applied: ["policy:pol_123"],
        failed: { policy: "Refused due to rule" },
      },
    };
    getSpy.mockImplementation(() =>
      Promise.resolve({
        data: settledProposal,
        error: undefined,
        response: new Response(),
      }),
    );

    renderWithClient(
      <TrainingPolicyProposalCard
        approval={{ ...baseApproval, approval_id: "apr_pol_settled", status: "approved" }}
        projectId="prj_1"
      />,
      createTestQueryClient(),
    );

    await waitFor(() => {
      expect(screen.getByText("policy:pol_123")).toBeInTheDocument();
      expect(screen.getByText(/Refused due to rule/)).toBeInTheDocument();
    });
  });
});
