import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { components } from "@/api/schema";
import { api, ApiError } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "../ApprovalCard";
import { ConnectionsProposalCard } from "./ConnectionsProposalCard";
import type { ProposalView } from "./useProposal";

type Findings = components["schemas"]["Findings"];

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

describe("ConnectionsProposalCard", () => {
  let getSpy: MockInstance;
  let postSpy: MockInstance;

  const baseApproval: ApprovalPart = {
    approval_id: "apr_conn_1",
    tool_call_id: "tc_1",
    status: "pending",
    proposal_kind: "connections",
    preview: {
      title: "Proposal: connections",
      kind: "text",
      body: "Fallback preview body text for connections",
    },
    note: null,
    type: "approval",
    kind: "tool",
  };

  const sampleProposal: ProposalView = {
    id: "prop_1",
    key: "connections/local_folder",
    kind: "connections",
    agent: "lead",
    approval_id: "apr_conn_1",
    status: "pending",
    created: "2026-10-29T10:00:00Z",
    turn_id: "turn_1",
    tool_call_id: "tc_1",
    warnings: ["Notice: external storage configured", "Verify write permissions"],
    slots: [],
    result: null,
    payload: {
      connections: [
        {
          name: "data_vault",
          kind: "local_folder",
          settings: { path: "/data/vault" },
          why: "Primary training data folder",
        },
        {
          name: "models_cache",
          kind: "local_folder",
          settings: { path: "/models/cache" },
          why: "Model weights cache directory",
        },
      ],
    },
  };

  const sampleFindings: Findings = {
    secret_files: [
      {
        path: ".env.production",
        reason: "Distinctive secret: contains production API keys",
      },
    ],
    unsupported_files: [
      {
        path: "corrupt_data.bin",
        reason: "Distinctive unsupported: unknown binary format",
      },
    ],
    truncated: false,
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

    postSpy = vi.spyOn(api, "POST").mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/connections/inspect") {
        return Promise.resolve({
          data: sampleFindings,
          response: new Response(JSON.stringify(sampleFindings), { status: 200 }),
        });
      }
      if (url === "/api/projects/{project_id}/approvals/{approval_id}/decision") {
        return Promise.resolve({
          data: {},
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });
  });

  afterEach(() => {
    getSpy.mockRestore();
    postSpy.mockRestore();
  });

  it("every connection renders with its reason", async () => {
    renderWithClient(<ConnectionsProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(await screen.findByText("data_vault")).toBeInTheDocument();
    expect(screen.getByText("Primary training data folder")).toBeInTheDocument();
    expect(screen.getByText("models_cache")).toBeInTheDocument();
    expect(screen.getByText("Model weights cache directory")).toBeInTheDocument();
  });

  it("each connection's inspect result is handed to ConnectionFindings", async () => {
    renderWithClient(<ConnectionsProposalCard approval={baseApproval} projectId="prj_1" />);

    // Assert that ConnectionFindings was rendered with the distinctive reasons verbatim
    expect(
      await screen.findAllByText("Distinctive secret: contains production API keys"),
    ).not.toHaveLength(0);
    expect(
      await screen.findAllByText("Distinctive unsupported: unknown binary format"),
    ).not.toHaveLength(0);

    // Verify inspect was called for each connection with kind and settings
    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/connections/inspect",
      expect.objectContaining({
        params: { path: { project_id: "prj_1" } },
        body: { kind: "local_folder", settings: { path: "/data/vault" } },
      }),
    );
    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/connections/inspect",
      expect.objectContaining({
        params: { path: { project_id: "prj_1" } },
        body: { kind: "local_folder", settings: { path: "/models/cache" } },
      }),
    );
  });

  it("an inspect error shows on its connection and the decision still posts", async () => {
    const user = userEvent.setup();
    postSpy.mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/connections/inspect") {
        return Promise.reject(new ApiError("inspect_failed", "Inspection permission denied", 400));
      }
      if (url === "/api/projects/{project_id}/approvals/{approval_id}/decision") {
        return Promise.resolve({
          data: {},
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderWithClient(<ConnectionsProposalCard approval={baseApproval} projectId="prj_1" />);

    // Error is displayed on connection
    expect(await screen.findAllByText("Inspection permission denied")).not.toHaveLength(0);

    // Decision controls are still enabled and posting works
    const approveBtn = screen.getByRole("button", { name: "Approve" });
    await user.click(approveBtn);

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "apr_conn_1" } },
        body: { approve: true, note: null },
      }),
    );
  });

  it("a pending card posts the decision with the note", async () => {
    const user = userEvent.setup();
    renderWithClient(<ConnectionsProposalCard approval={baseApproval} projectId="prj_1" />);

    await screen.findByText("data_vault");
    const noteInput = screen.getByLabelText(/note/i);
    await user.type(noteInput, "Approved local connections");
    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "apr_conn_1" } },
        body: { approve: true, note: "Approved local connections" },
      }),
    );
  });

  it("renders warnings as given above controls", async () => {
    renderWithClient(<ConnectionsProposalCard approval={baseApproval} projectId="prj_1" />);

    expect(await screen.findByText("Notice: external storage configured")).toBeInTheDocument();
    expect(screen.getByText("Verify write permissions")).toBeInTheDocument();
  });

  it("a failed proposal load shows the fallback preview and still lets the user decide", async () => {
    const user = userEvent.setup();
    getSpy.mockRejectedValue(new ApiError("load_failed", "Failed to fetch proposal", 500));

    renderWithClient(<ConnectionsProposalCard approval={baseApproval} projectId="prj_1" />);

    // Fallback preview body is displayed
    expect(
      await screen.findByText("Fallback preview body text for connections"),
    ).toBeInTheDocument();

    // User can still decide
    const approveBtn = screen.getByRole("button", { name: "Approve" });
    await user.click(approveBtn);

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "apr_conn_1" } },
        body: { approve: true, note: null },
      }),
    );
  });

  it("a decided card shows status, applied and failed parts, no controls and no inspect call", async () => {
    const decidedApproval: ApprovalPart = {
      ...baseApproval,
      status: "approved",
    };
    const decidedProposal: ProposalView = {
      ...sampleProposal,
      status: "approved",
      result: {
        applied: ["data_vault"],
        failed: {
          models_cache: "Permission denied",
        },
      },
    };

    getSpy.mockResolvedValue({
      data: decidedProposal,
      response: new Response(JSON.stringify(decidedProposal), { status: 200 }),
    });

    renderWithClient(<ConnectionsProposalCard approval={decidedApproval} projectId="prj_1" />);

    // Status badge is rendered
    expect(await screen.findByText("approved")).toBeInTheDocument();

    // Applied and failed parts from result are displayed
    expect(await screen.findByText("Applied:")).toBeInTheDocument();
    expect(screen.getByText("Failed:")).toBeInTheDocument();
    expect(screen.getAllByText("data_vault")).toHaveLength(2);
    expect(screen.getAllByText("models_cache")).toHaveLength(2);
    expect(screen.getByText(/Permission denied/)).toBeInTheDocument();

    // No approval controls
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();

    // Inspect was not called
    expect(postSpy).not.toHaveBeenCalledWith(
      "/api/projects/{project_id}/connections/inspect",
      expect.anything(),
    );
  });

  it("a superseded card shows its status", async () => {
    const supersededApproval: ApprovalPart = {
      ...baseApproval,
      status: "cancelled",
    };
    const supersededProposal: ProposalView = {
      ...sampleProposal,
      status: "superseded",
    };

    getSpy.mockResolvedValue({
      data: supersededProposal,
      response: new Response(JSON.stringify(supersededProposal), { status: 200 }),
    });

    renderWithClient(<ConnectionsProposalCard approval={supersededApproval} projectId="prj_1" />);

    // Shows superseded badge
    expect(await screen.findByText("superseded")).toBeInTheDocument();

    // No controls and no inspect
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(postSpy).not.toHaveBeenCalledWith(
      "/api/projects/{project_id}/connections/inspect",
      expect.anything(),
    );
  });

  it("the dispatcher renders ConnectionsProposalCard for proposal_kind connections and generic card for other", async () => {
    const otherApproval: ApprovalPart = {
      ...baseApproval,
      proposal_kind: "other",
      preview: {
        title: "Generic tool approval",
        kind: "text",
        body: "Generic preview body text",
      },
    };

    const { rerender } = renderWithClient(
      <ApprovalCard approval={baseApproval} projectId="prj_1" />,
    );

    // ConnectionsProposalCard is rendered (fetches proposal)
    await waitFor(() => {
      expect(getSpy).toHaveBeenCalledWith(
        "/api/projects/{project_id}/proposals/by-approval/{approval_id}",
        expect.anything(),
      );
    });

    // Rerender with 'other' proposal kind
    rerender(
      <QueryClientProvider client={createTestQueryClient()}>
        <ApprovalCard approval={otherApproval} projectId="prj_1" />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Generic tool approval")).toBeInTheDocument();
    expect(screen.getByText("Generic preview body text")).toBeInTheDocument();
  });
});
