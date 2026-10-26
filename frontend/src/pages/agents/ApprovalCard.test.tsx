import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "./ApprovalCard";

describe("ApprovalCard", () => {
  let postSpy: MockInstance | undefined;

  beforeEach(() => {
    postSpy = vi.spyOn(api, "POST").mockImplementation(() => {
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });
  });

  afterEach(() => {
    postSpy?.mockRestore();
  });

  const baseApproval: ApprovalPart = {
    approval_id: "appr_123",
    tool_call_id: "tc_1",
    status: "pending",
    preview: {
      title: "Write NOTES.md",
      kind: "text",
      body: "Sample note text",
    },
    note: null,
    type: "approval",
  };

  it("renders text kind preview", () => {
    render(<ApprovalCard approval={baseApproval} toolName="Write" projectId="prj_1" />);

    expect(screen.getByText("Write")).toBeInTheDocument();
    expect(screen.getByText("Write NOTES.md")).toBeInTheDocument();
    expect(screen.getByText("Sample note text")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });

  it("renders command kind preview as a code block", () => {
    const cmdApproval: ApprovalPart = {
      ...baseApproval,
      preview: {
        title: "Run pytest",
        kind: "command",
        body: "pytest tests/test_flow.py",
      },
    };

    render(<ApprovalCard approval={cmdApproval} toolName="Bash" projectId="prj_1" />);

    expect(screen.getByText("Bash")).toBeInTheDocument();
    expect(screen.getByText("Run pytest")).toBeInTheDocument();
    expect(screen.getByText("pytest tests/test_flow.py")).toBeInTheDocument();
  });

  it("renders diff kind preview with colored added and removed lines", () => {
    const diffBody = [
      "--- a/file.py",
      "+++ b/file.py",
      "@@ -1,3 +1,3 @@",
      " def foo():",
      "-    return 1",
      "+    return 2",
    ].join("\n");

    const diffApproval: ApprovalPart = {
      ...baseApproval,
      preview: {
        title: "Edit file.py",
        kind: "diff",
        body: diffBody,
      },
    };

    render(<ApprovalCard approval={diffApproval} toolName="Edit" projectId="prj_1" />);

    expect(screen.getByText("Edit")).toBeInTheDocument();
    expect(screen.getByText("Edit file.py")).toBeInTheDocument();
    expect(screen.getByText("+ return 2")).toBeInTheDocument();
    expect(screen.getByText("- return 1")).toBeInTheDocument();
  });

  it("calls the decision route with approve: true when clicking Approve", async () => {
    const user = userEvent.setup();
    render(<ApprovalCard approval={baseApproval} toolName="Write" projectId="prj_1" />);

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "appr_123" } },
        body: { approve: true, note: null },
      }),
    );
  });

  it("calls the decision route with approve: false and optional note when clicking Reject", async () => {
    const user = userEvent.setup();
    render(<ApprovalCard approval={baseApproval} toolName="Write" projectId="prj_1" />);

    const noteInput = screen.getByLabelText("Rejection note");
    await user.type(noteInput, "Missing unit tests");
    await user.click(screen.getByRole("button", { name: "Reject" }));

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "appr_123" } },
        body: { approve: false, note: "Missing unit tests" },
      }),
    );
  });

  it("renders decided statuses: approved, rejected with note, cancelled, expired", () => {
    const { rerender } = render(
      <ApprovalCard
        approval={{ ...baseApproval, status: "approved" }}
        toolName="Write"
        projectId="prj_1"
      />,
    );
    expect(screen.getByText("approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();

    rerender(
      <ApprovalCard
        approval={{
          ...baseApproval,
          status: "rejected",
          note: "Please reformat diff",
        }}
        toolName="Write"
        projectId="prj_1"
      />,
    );
    expect(screen.getByText("rejected")).toBeInTheDocument();
    expect(screen.getByText("Please reformat diff")).toBeInTheDocument();

    rerender(
      <ApprovalCard
        approval={{ ...baseApproval, status: "cancelled" }}
        toolName="Write"
        projectId="prj_1"
      />,
    );
    expect(screen.getByText("cancelled")).toBeInTheDocument();

    rerender(
      <ApprovalCard
        approval={{ ...baseApproval, status: "expired" }}
        toolName="Write"
        projectId="prj_1"
      />,
    );
    expect(screen.getByText("expired")).toBeInTheDocument();
  });

  it("shows error and re-enables buttons on failed decision, without changing card status before message.updated", async () => {
    const user = userEvent.setup();
    postSpy?.mockRejectedValueOnce(new Error("Network failed"));

    render(<ApprovalCard approval={baseApproval} toolName="Write" projectId="prj_1" />);

    const approveButton = screen.getByRole("button", { name: "Approve" });
    const rejectButton = screen.getByRole("button", { name: "Reject" });

    await user.click(approveButton);

    // Error is shown
    const errorAlert = await screen.findByRole("alert");
    expect(errorAlert).toBeInTheDocument();

    // Buttons are re-enabled
    expect(approveButton).toBeEnabled();
    expect(rejectButton).toBeEnabled();

    // Card status does not change optimistically before message.updated
    expect(screen.queryByText("approved")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });
});
