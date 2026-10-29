import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";
import { MessageView } from "./MessageView";
import type { MessageView as MessageViewType } from "./session/events";

// Messages show on a project's Agents page; an approval card needs that project to act.
function renderInProject(element: ReactElement) {
  const router = createMemoryRouter([{ path: "/projects/:projectId/agents", element }], {
    initialEntries: ["/projects/prj_1/agents"],
  });
  return render(<RouterProvider router={router} />);
}

describe("MessageView approvals", () => {
  it("renders approval part where it stands in the message parts", () => {
    const agentMsg: MessageViewType = {
      id: "a8",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        { type: "text", text: "I need your approval to run this." },
        {
          type: "approval",
          kind: "tool",
          approval_id: "apr_1",
          tool_call_id: "c8",
          preview: { kind: "command", title: "Run command", body: "rm -rf build" },
          status: "pending",
          note: null,
        },
      ],
      status: "waiting",
      created_at: "2026-10-21T18:00:08Z",
    };

    renderInProject(<MessageView message={agentMsg} />);
    expect(screen.getByText("I need your approval to run this.")).toBeInTheDocument();
    expect(screen.getByText("Run command")).toBeInTheDocument();
    expect(screen.getByText("rm -rf build")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("renders the approval card next to its matching tool call", () => {
    const agentMsg: MessageViewType = {
      id: "a9",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "tool_call",
          id: "call_1",
          name: "Write",
          input: { file_path: "NOTES.md", content: "Test" },
        },
        {
          type: "approval",
          kind: "tool",
          approval_id: "apr_99",
          tool_call_id: "call_1",
          preview: { kind: "text", title: "Write NOTES.md", body: "File content preview" },
          status: "pending",
          note: null,
        },
      ],
      status: "waiting",
      created_at: "2026-10-21T18:00:10Z",
    };

    renderInProject(<MessageView message={agentMsg} />);

    // Tool step and approval card render together
    expect(screen.getByRole("button", { name: /Write.*NOTES\.md/i })).toBeInTheDocument();
    expect(screen.getByText("File content preview")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();

    // Waiting line in place of typing indicator
    expect(screen.getByText("Waiting for your approval")).toBeInTheDocument();
  });
});
