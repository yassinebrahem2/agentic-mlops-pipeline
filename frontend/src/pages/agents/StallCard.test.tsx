import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "./ApprovalCard";

const stall: ApprovalPart = {
  type: "approval",
  approval_id: "apr_stall",
  tool_call_id: null,
  status: "pending",
  preview: {
    title: "Stalled: no usable key",
    kind: "text",
    body: "Key 1: daily limit reached (since 12:00 UTC). Key 2: billing, out of credit. Key 3: rejected by the provider.",
  },
  note: null,
  kind: "stall",
  step: 2,
  proposal_kind: null,
};

const decisionRoute = "/api/projects/{project_id}/approvals/{approval_id}/decision";

describe("StallCard", () => {
  let postSpy: MockInstance;

  beforeEach(() => {
    postSpy = vi
      .spyOn(api, "POST")
      .mockImplementation(() =>
        Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) }),
      );
  });

  afterEach(() => {
    postSpy.mockRestore();
  });

  it("shows the title and one row per key with its state and reason, never the raw body", () => {
    render(<ApprovalCard approval={stall} projectId="prj_1" />);

    expect(screen.getByText("Stalled: no usable key")).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Key 1");
    expect(rows[0]).toHaveTextContent("daily limit reached (since 12:00 UTC)");
    expect(rows[1]).toHaveTextContent("Key 2");
    expect(rows[1]).toHaveTextContent("billing, out of credit");
    expect(rows[2]).toHaveTextContent("Key 3");
    expect(rows[2]).toHaveTextContent("rejected by the provider");
    expect(screen.queryByText(stall.preview.body)).not.toBeInTheDocument();
  });

  it("renders a row without a key label as it is", () => {
    render(
      <ApprovalCard
        approval={{
          ...stall,
          preview: { ...stall.preview, body: "Every key is resting.\nKey 1: ok." },
        }}
        projectId="prj_1"
      />,
    );

    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Every key is resting.");
    expect(rows[1]).toHaveTextContent("Key 1");
    expect(rows[1]).toHaveTextContent("ok");
  });

  it("Try again approves the stall and both buttons disable while it is pending", async () => {
    let resolve: (() => void) | undefined;
    postSpy.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = () => {
            done({ data: {}, response: new Response(null, { status: 200 }) });
          };
        }),
    );
    const user = userEvent.setup();
    render(<ApprovalCard approval={stall} projectId="prj_1" />);

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(postSpy).toHaveBeenCalledWith(
      decisionRoute,
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "apr_stall" } },
        body: { approve: true, note: null },
      }),
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    resolve?.();
  });

  it("Stop rejects the stall", async () => {
    const user = userEvent.setup();
    render(<ApprovalCard approval={stall} projectId="prj_1" />);

    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(postSpy).toHaveBeenCalledWith(
      decisionRoute,
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "apr_stall" } },
        body: { approve: false, note: null },
      }),
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Try again" })).toBeDisabled();
    });
  });

  it("reads Resumed once approved and Stopped once rejected or cancelled, without buttons", () => {
    const { rerender } = render(
      <ApprovalCard approval={{ ...stall, status: "approved" }} projectId="prj_1" />,
    );
    expect(screen.getByText("Resumed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();

    rerender(<ApprovalCard approval={{ ...stall, status: "rejected" }} projectId="prj_1" />);
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();

    rerender(<ApprovalCard approval={{ ...stall, status: "cancelled" }} projectId="prj_1" />);
    expect(screen.getByText("Stopped")).toBeInTheDocument();
  });

  it("the dispatcher picks the stall card for a stall and the tool card for a tool", () => {
    const { rerender } = render(<ApprovalCard approval={stall} projectId="prj_1" />);
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();

    rerender(
      <ApprovalCard
        approval={{
          ...stall,
          approval_id: "apr_tool",
          kind: "tool",
          tool_call_id: "tc_1",
          step: null,
          preview: { title: "Write NOTES.md", kind: "text", body: "Sample note text" },
        }}
        toolName="Write"
        projectId="prj_1"
      />,
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});
