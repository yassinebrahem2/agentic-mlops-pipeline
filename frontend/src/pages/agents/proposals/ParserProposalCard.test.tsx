import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "../ApprovalCard";
import { ParserProposalCard } from "./ParserProposalCard";
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

describe("ParserProposalCard", () => {
  let getSpy: MockInstance;

  const baseApproval: ApprovalPart = {
    approval_id: "apr_parser_1",
    tool_call_id: "tc_p1",
    status: "pending",
    proposal_kind: "parser",
    preview: {
      title: "Proposal: parser",
      kind: "text",
      body: "Fallback preview body text for parser",
    },
    note: null,
    type: "approval",
    kind: "tool",
  };

  const sampleMappingProposal: ProposalView = {
    id: "prop_p1",
    key: "file:conn_data:logs/*.jsonl",
    kind: "parser",
    agent: "lead",
    approval_id: "apr_parser_1",
    status: "pending",
    created: "2026-10-30T10:00:00Z",
    turn_id: "turn_1",
    tool_call_id: "tc_p1",
    warnings: [],
    slots: [],
    result: null,
    payload: {
      spec: {
        source_id: "file:conn_data:logs/*.jsonl",
        mapping: {
          format: "jsonl",
          fields: {
            check_id: "metric.name",
            value: "metric.val",
            ran_at: "ts",
          },
          pattern: null,
          fired_when: {
            op: "gt",
            field: "value",
            value: 0.5,
          },
          defaults: {
            category: "model_performance",
            subject: "model_churn",
          },
        },
        code: null,
      },
      sample_input: '{"metric": {"name": "loss", "val": 0.6}, "ts": "2026-10-30T10:00:00Z"}\n',
      sample_records: [
        {
          check_id: "loss",
          origin: "team",
          ran_at: "2026-10-30T10:00:00Z",
          fired: true,
          value: 0.6,
          threshold: 0.5,
          category: "model_performance",
          subject: "model_churn",
          model: null,
          detail: {},
          source_ref: "logs/001.jsonl",
          version: null,
          not_enough_data: false,
        },
      ],
    },
  };

  const sampleCodeProposal: ProposalView = {
    id: "prop_p2",
    key: "file:conn_data:custom/*.raw",
    kind: "parser",
    agent: "lead",
    approval_id: "apr_parser_2",
    status: "pending",
    created: "2026-10-30T10:00:00Z",
    turn_id: "turn_2",
    tool_call_id: "tc_p2",
    warnings: [],
    slots: [],
    result: null,
    payload: {
      spec: {
        source_id: "file:conn_data:custom/*.raw",
        mapping: null,
        code: {
          script: "import sys\nprint('parsed')",
          timeout_s: 20,
        },
      },
      sample_input: "custom binary or text raw header",
      sample_records: [],
    },
  };

  beforeEach(() => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path) => {
      if (path === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: sampleMappingProposal,
          response: new Response(JSON.stringify(sampleMappingProposal), { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders source, mapping details, sample input and sample records", async () => {
    renderWithClient(<ParserProposalCard approval={baseApproval} projectId="prj_test" />);

    expect(await screen.findByText("file:conn_data:logs/*.jsonl")).toBeInTheDocument();
    expect(screen.getByText("Format:")).toBeInTheDocument();
    expect(screen.getByText("jsonl")).toBeInTheDocument();
    expect(screen.getByText("Sample Input")).toBeInTheDocument();
    expect(screen.getByText(/metric.*loss.*val.*0.6/)).toBeInTheDocument();
    expect(screen.getByText("Sample Records")).toBeInTheDocument();
    expect(screen.getByText("loss")).toBeInTheDocument();
  });

  it("renders script and timeout for code parser", async () => {
    getSpy.mockImplementation(() =>
      Promise.resolve({
        data: sampleCodeProposal,
        response: new Response(JSON.stringify(sampleCodeProposal), { status: 200 }),
      }),
    );

    renderWithClient(
      <ParserProposalCard
        approval={{ ...baseApproval, approval_id: "apr_parser_2" }}
        projectId="prj_test"
      />,
    );

    expect(await screen.findByText("file:conn_data:custom/*.raw")).toBeInTheDocument();
    expect(screen.getByText("Script:")).toBeInTheDocument();
    expect(screen.getByText(/print\('parsed'\)/)).toBeInTheDocument();
    expect(screen.getByText("Timeout:")).toBeInTheDocument();
    expect(screen.getByText(/20s/)).toBeInTheDocument();
  });

  it("is dispatched by ApprovalCard for proposal_kind 'parser'", async () => {
    renderWithClient(<ApprovalCard approval={baseApproval} projectId="prj_test" />);

    expect(await screen.findByText("file:conn_data:logs/*.jsonl")).toBeInTheDocument();
  });
});
