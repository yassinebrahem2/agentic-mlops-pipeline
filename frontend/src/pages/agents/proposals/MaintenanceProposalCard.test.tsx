import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "../ApprovalCard";
import { MaintenanceProposalCard } from "./MaintenanceProposalCard";
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

describe("MaintenanceProposalCard", () => {
  let getSpy: MockInstance;
  let postSpy: MockInstance;

  const baseApproval: ApprovalPart = {
    approval_id: "apr_maint_1",
    tool_call_id: "tc_1",
    status: "pending",
    proposal_kind: "maintenance",
    preview: {
      title: "Proposal: maintenance",
      kind: "text",
      body: "Fallback preview body text for maintenance",
    },
    note: null,
    type: "approval",
    kind: "tool",
  };

  const sampleProposal: ProposalView = {
    id: "prop_maint_1",
    key: "window_key",
    kind: "maintenance",
    agent: "lead",
    approval_id: "apr_maint_1",
    status: "pending",
    created: "2026-10-31T10:00:00Z",
    turn_id: "turn_1",
    tool_call_id: "tc_1",
    warnings: [],
    slots: [],
    result: null,
    payload: {
      window: {
        start: "2026-11-01T00:00:00Z",
        end: "2026-11-01T04:00:00Z",
        scope: ["feed:orders", "con_warehouse"],
        reason: "Database maintenance and upgrade",
      },
    },
  };

  beforeEach(() => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: sampleProposal,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    postSpy = vi.spyOn(api, "POST").mockImplementation(() => {
      return Promise.resolve({
        data: {},
        response: new Response(null, { status: 200 }),
      });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders window details (dates, scope, reason) when loaded", async () => {
    renderWithClient(<MaintenanceProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(await screen.findByText("2026-11-01T00:00:00Z")).toBeInTheDocument();
    expect(screen.getByText("2026-11-01T04:00:00Z")).toBeInTheDocument();
    expect(screen.getByText("feed:orders")).toBeInTheDocument();
    expect(screen.getByText("con_warehouse")).toBeInTheDocument();
    expect(screen.getByText("Database maintenance and upgrade")).toBeInTheDocument();
    expect(getSpy).toHaveBeenCalled();
  });

  it("posts approval decision when Approve button is clicked", async () => {
    const user = userEvent.setup();
    renderWithClient(<MaintenanceProposalCard approval={baseApproval} projectId="prj_1" />);

    await screen.findByText("Database maintenance and upgrade");
    const approveBtn = screen.getByRole("button", { name: "Approve" });
    await user.click(approveBtn);

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "apr_maint_1" } },
        body: { approve: true, note: null },
      }),
    );
  });

  it("posts reject decision with note when Reject button is clicked", async () => {
    const user = userEvent.setup();
    renderWithClient(<MaintenanceProposalCard approval={baseApproval} projectId="prj_1" />);

    await screen.findByText("Database maintenance and upgrade");
    const noteInput = screen.getByLabelText(/note/i);
    await user.type(noteInput, "Window overlaps with peak hours");
    const rejectBtn = screen.getByRole("button", { name: "Reject" });
    await user.click(rejectBtn);

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "apr_maint_1" } },
        body: { approve: false, note: "Window overlaps with peak hours" },
      }),
    );
  });

  it("renders through ApprovalCard component via CARD_FOR_PROPOSAL dispatch", async () => {
    renderWithClient(<ApprovalCard approval={baseApproval} projectId="prj_1" />);

    expect(await screen.findByText("Database maintenance and upgrade")).toBeInTheDocument();
    expect(screen.getByText("feed:orders")).toBeInTheDocument();
  });
});
