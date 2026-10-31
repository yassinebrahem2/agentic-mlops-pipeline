import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "../ApprovalCard";
import { RuleProposalCard } from "./RuleProposalCard";
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

describe("RuleProposalCard", () => {
  let getSpy: MockInstance;

  const baseApproval: ApprovalPart = {
    approval_id: "apr_rule_1",
    tool_call_id: "tc_1",
    status: "pending",
    proposal_kind: "rule",
    preview: {
      title: "Proposal: rule",
      kind: "text",
      body: "Fallback preview body text for rule proposal",
    },
    note: null,
    type: "approval",
    kind: "tool",
  };

  const sampleProposal: ProposalView = {
    id: "prop_rule_1",
    key: "churn:auto-rollback",
    kind: "rule",
    agent: "lead",
    approval_id: "apr_rule_1",
    status: "pending",
    created: "2026-10-31T05:00:00Z",
    turn_id: "turn_1",
    tool_call_id: "tc_1",
    warnings: ["Rule hook 'hok_rollback' deploys."],
    slots: [],
    result: null,
    payload: {
      spec: {
        name: "auto-rollback",
        why: "Roll back when drift is high",
        model: "churn",
        hook_id: "hok_rollback",
        hook_kind: "rollback",
        limits: {
          per_incident: 1,
          per_day: 2,
        },
        terms: [
          {
            check_id: "drift_check",
            model: "churn",
            when: { op: "gt", field: "value", value: 0.5 },
            window_minutes: 60,
            quantifier: "latest",
            min_records: 1,
          },
          {
            check_id: "latency_check",
            model: null,
            when: { op: "ge", field: "p99", value: 200 },
            window_minutes: 30,
            quantifier: "all",
            min_records: 3,
          },
        ],
      },
    },
  };

  beforeEach(() => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: sampleProposal,
          response: new Response(JSON.stringify(sampleProposal), { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });
  });

  afterEach(() => {
    getSpy.mockRestore();
  });

  it("renders rule details including name, model, why, hook, limits, and terms", async () => {
    renderWithClient(<RuleProposalCard approval={baseApproval} projectId="prj_1" />);

    // Rule header info
    expect(await screen.findByText("auto-rollback")).toBeInTheDocument();
    expect(screen.getByText("Roll back when drift is high")).toBeInTheDocument();
    expect(screen.getAllByText("churn")).toHaveLength(2);

    // Hook and limits
    expect(screen.getByText("hok_rollback")).toBeInTheDocument();
    expect(screen.getByText("rollback")).toBeInTheDocument();
    expect(screen.getByText("1 per incident, 2 per day")).toBeInTheDocument();

    // Term 1 (with model)
    expect(screen.getByText("drift_check")).toBeInTheDocument();
    expect(screen.getByText("value gt 0.5")).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.textContent === "Window: 60m")).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.textContent === "Min records: 1")).toBeInTheDocument();
    expect(screen.getByText("latest")).toBeInTheDocument();

    // Term 2 (project-wide model)
    expect(screen.getByText("latency_check")).toBeInTheDocument();
    expect(screen.getByText("p99 ge 200")).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.textContent === "Window: 30m")).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.textContent === "Min records: 3")).toBeInTheDocument();
    expect(screen.getByText("all")).toBeInTheDocument();
    expect(screen.getByText("project-wide")).toBeInTheDocument();
  });

  it("renders warnings when present on the proposal", async () => {
    renderWithClient(<RuleProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(await screen.findByText("Rule hook 'hok_rollback' deploys.")).toBeInTheDocument();
  });

  it("renders fallback preview text when proposal fails to load", async () => {
    getSpy.mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.reject(new Error("Network error"));
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderWithClient(<RuleProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(
      await screen.findByText("Fallback preview body text for rule proposal"),
    ).toBeInTheDocument();
  });

  it("ApprovalCard selects RuleProposalCard for rule proposals", async () => {
    renderWithClient(<ApprovalCard approval={baseApproval} projectId="prj_1" />);

    await waitFor(() => {
      expect(getSpy).toHaveBeenCalledWith(
        "/api/projects/{project_id}/proposals/by-approval/{approval_id}",
        expect.anything(),
      );
    });

    expect(await screen.findByText("auto-rollback")).toBeInTheDocument();
    expect(screen.getByText("hok_rollback")).toBeInTheDocument();
  });
});
