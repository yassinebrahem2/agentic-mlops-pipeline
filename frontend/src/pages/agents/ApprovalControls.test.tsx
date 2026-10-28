import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { ApprovalControls } from "./ApprovalControls";

describe("ApprovalControls", () => {
  let postSpy: MockInstance | undefined;

  beforeEach(() => {
    postSpy = vi.spyOn(api, "POST").mockImplementation(() => {
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });
  });

  afterEach(() => {
    postSpy?.mockRestore();
  });

  it("says Approve and Reject by default", () => {
    render(<ApprovalControls approvalId="apr_1" projectId="prj_1" />);

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });

  it("uses the given labels and posts the same decision body", async () => {
    const user = userEvent.setup();
    render(
      <ApprovalControls
        approvalId="apr_stall"
        projectId="prj_1"
        approveLabel="Try again"
        rejectLabel="Stop"
      />,
    );

    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/note/i), "  waited  ");
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "apr_stall" } },
        body: { approve: true, note: "waited" },
      }),
    );
  });

  it("posts a rejection from the reject button whatever its label", async () => {
    const user = userEvent.setup();
    render(<ApprovalControls approvalId="apr_stall" projectId="prj_1" rejectLabel="Stop" />);

    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({ body: { approve: false, note: null } }),
    );
  });
});
