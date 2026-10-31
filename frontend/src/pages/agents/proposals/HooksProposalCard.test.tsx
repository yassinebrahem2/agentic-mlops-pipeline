import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "../ApprovalCard";
import { HooksProposalCard } from "./HooksProposalCard";
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

describe("HooksProposalCard", () => {
  let getSpy: MockInstance;

  const baseApproval: ApprovalPart = {
    approval_id: "apr_hooks_1",
    tool_call_id: "tc_1",
    status: "pending",
    proposal_kind: "hooks",
    preview: {
      title: "Proposal: hooks",
      kind: "text",
      body: "Fallback preview body text for hooks proposal",
    },
    note: null,
    type: "approval",
    kind: "tool",
  };

  const sampleProposal: ProposalView = {
    id: "prop_hooks_1",
    key: "project",
    kind: "hooks",
    agent: "lead",
    approval_id: "apr_hooks_1",
    status: "pending",
    created: "2026-10-31T05:00:00Z",
    turn_id: "turn_1",
    tool_call_id: "tc_1",
    warnings: ["Hook 'deploy-candidate' (model: churn) deploys."],
    slots: [],
    result: null,
    payload: {
      hooks: [
        {
          name: "train-runner",
          model: "churn",
          why: "Train the churn prediction model",
          spec: {
            kind: "train",
            type: "command",
            template: "python train.py --model {model}",
            workflow: null,
            inputs: {},
            placeholders: ["model"],
            entry_files: ["train.py"],
            deploys: false,
          },
        },
        {
          name: "deploy-candidate",
          model: "churn",
          why: "Deploy the trained candidate model",
          spec: {
            kind: "deploy",
            type: "ci_dispatch",
            template: null,
            workflow: "deploy.yml",
            inputs: { env: "prod", cluster: "us-central1" },
            placeholders: [],
            entry_files: [],
            deploys: true,
          },
        },
        {
          name: "global-eval",
          model: null,
          why: "Project-wide evaluation check",
          spec: {
            kind: "evaluate",
            type: "command",
            template: "pytest tests/eval.py",
            workflow: null,
            inputs: {},
            placeholders: [],
            entry_files: ["tests/eval.py"],
            deploys: false,
          },
        },
      ],
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

  it("renders both command and ci_dispatch hooks with all required fields", async () => {
    renderWithClient(<HooksProposalCard approval={baseApproval} projectId="prj_1" />);

    // Command hook with model
    expect(await screen.findByText("train-runner")).toBeInTheDocument();
    expect(screen.getByText("Train the churn prediction model")).toBeInTheDocument();
    expect(screen.getByText("python train.py --model {model}")).toBeInTheDocument();
    expect(screen.getByText("model")).toBeInTheDocument(); // placeholder
    expect(screen.getByText("train.py")).toBeInTheDocument(); // entry file

    // CI dispatch hook with deploys
    expect(screen.getByText("deploy-candidate")).toBeInTheDocument();
    expect(screen.getByText("Deploy the trained candidate model")).toBeInTheDocument();
    expect(screen.getByText("deploy.yml")).toBeInTheDocument();
    expect(screen.getByText("env=prod, cluster=us-central1")).toBeInTheDocument();

    // Global hook (model is null -> project-wide)
    expect(screen.getByText("global-eval")).toBeInTheDocument();
    expect(screen.getByText("project-wide")).toBeInTheDocument();
    expect(screen.getByText("pytest tests/eval.py")).toBeInTheDocument();
  });

  it("renders warnings when present on the proposal", async () => {
    renderWithClient(<HooksProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(
      await screen.findByText("Hook 'deploy-candidate' (model: churn) deploys."),
    ).toBeInTheDocument();
  });

  it("renders fallback preview text when proposal fails to load", async () => {
    getSpy.mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.reject(new Error("Network error"));
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderWithClient(<HooksProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(
      await screen.findByText("Fallback preview body text for hooks proposal"),
    ).toBeInTheDocument();
  });

  it("ApprovalCard selects HooksProposalCard for hooks proposals", async () => {
    renderWithClient(<ApprovalCard approval={baseApproval} projectId="prj_1" />);

    await waitFor(() => {
      expect(getSpy).toHaveBeenCalledWith(
        "/api/projects/{project_id}/proposals/by-approval/{approval_id}",
        expect.anything(),
      );
    });

    expect(await screen.findByText("train-runner")).toBeInTheDocument();
    expect(screen.getByText("deploy-candidate")).toBeInTheDocument();
  });

  it("renders applied and failed results when proposal has results", async () => {
    const proposalWithResult: ProposalView = {
      ...sampleProposal,
      status: "approved",
      result: {
        applied: ["hook:churn:train-runner", "hook:global-eval"],
        failed: {
          "hook:churn:deploy-candidate": "CI credentials missing",
        },
      },
    };

    getSpy.mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: proposalWithResult,
          response: new Response(JSON.stringify(proposalWithResult), { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderWithClient(<HooksProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(
      await screen.findByText("hook:churn:train-runner, hook:global-eval"),
    ).toBeInTheDocument();
    expect(screen.getByText("hook:churn:deploy-candidate")).toBeInTheDocument();
    expect(screen.getByText(/CI credentials missing/)).toBeInTheDocument();
  });
});
