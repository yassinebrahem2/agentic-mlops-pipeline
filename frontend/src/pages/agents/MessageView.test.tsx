import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { MessageView } from "./MessageView";
import type { MessageView as MessageViewType } from "./session/events";

describe("MessageView", () => {
  it("renders user message and queued mark", () => {
    const userMsg: MessageViewType = {
      id: "u1",
      turn_id: null,
      author_kind: "user",
      author_name: "user",
      from_agent: null,
      parts: [{ type: "text", text: "What is the project about?" }],
      status: "queued",
      created_at: "2026-10-21T18:00:00Z",
    };

    render(<MessageView message={userMsg} />);
    expect(screen.getByText("What is the project about?")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
  });

  it("renders agent markdown answer and tool step with summary, input, and result", async () => {
    const user = userEvent.setup();
    const agentMsg: MessageViewType = {
      id: "a1",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "tool_call",
          id: "c1",
          name: "Read",
          input: { file_path: "README.md" },
        },
        {
          type: "tool_result",
          tool_call_id: "c1",
          content: "1\t# Demo Project\n2\tMLOps agents.",
          is_error: false,
        },
        {
          type: "text",
          text: "Here is the **README** content for you.",
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:01Z",
    };

    render(<MessageView message={agentMsg} />);

    // Tool step closed by default with summary
    expect(screen.getByRole("button", { name: /Read README\.md/i })).toBeInTheDocument();
    expect(screen.queryByText(/Demo Project/)).not.toBeInTheDocument();

    // Markdown rendered
    expect(screen.getByText("README")).toBeInTheDocument();
    expect(screen.getByText(/Here is the/)).toBeInTheDocument();

    // Open tool step
    await user.click(screen.getByRole("button", { name: /Read README\.md/i }));
    expect(screen.getByText(/Demo Project/)).toBeInTheDocument();
  });

  it("displays 'Running' when a tool call has not completed", () => {
    const agentMsg: MessageViewType = {
      id: "a2",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "tool_call",
          id: "c2",
          name: "Grep",
          input: { pattern: "TODO" },
        },
      ],
      status: "streaming",
      created_at: "2026-10-21T18:00:02Z",
    };

    render(<MessageView message={agentMsg} />);
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("styles tool result errors as error", async () => {
    const user = userEvent.setup();
    const agentMsg: MessageViewType = {
      id: "a3",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "tool_call",
          id: "c3",
          name: "Read",
          input: { file_path: "missing.txt" },
        },
        {
          type: "tool_result",
          tool_call_id: "c3",
          content: "File not found",
          is_error: true,
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:03Z",
    };

    render(<MessageView message={agentMsg} />);
    await user.click(screen.getByRole("button", { name: /Read missing\.txt/i }));
    const errorOutput = screen.getByText("File not found");
    expect(errorOutput).toBeInTheDocument();
    expect(errorOutput.closest("[data-error='true']")).not.toBeNull();
  });

  it("treats model output as untrusted and sanitizes HTML and javascript: URLs", () => {
    const agentMsg: MessageViewType = {
      id: "a4",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "text",
          text: "Malicious <script>alert('xss')</script> and <img src=x onerror=alert(1)> and [bad link](javascript:alert(1))",
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:04Z",
    };

    const { container } = render(<MessageView message={agentMsg} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    const link = screen.getByText("bad link");
    expect(link.getAttribute("href") ?? "").not.toContain("javascript:");
  });

  it("marks stopped and interrupted agent answers", () => {
    const stoppedMsg: MessageViewType = {
      id: "a5",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [{ type: "text", text: "Partial answer before stop." }],
      status: "stopped",
      created_at: "2026-10-21T18:00:05Z",
    };

    const { rerender } = render(<MessageView message={stoppedMsg} />);
    expect(screen.getByText("Stopped")).toBeInTheDocument();

    const interruptedMsg: MessageViewType = {
      ...stoppedMsg,
      id: "a6",
      status: "interrupted",
    };
    rerender(<MessageView message={interruptedMsg} />);
    expect(screen.getByText("Interrupted: the app stopped during this answer")).toBeInTheDocument();
  });

  it("renders system messages as notices across the column", () => {
    const sysMsg: MessageViewType = {
      id: "s1",
      turn_id: "trn_1",
      author_kind: "system",
      author_name: "system",
      from_agent: null,
      parts: [{ type: "text", text: "Agent run failed: out of memory." }],
      status: "complete",
      created_at: "2026-10-21T18:00:06Z",
    };

    render(<MessageView message={sysMsg} />);
    const notice = screen.getByText("Agent run failed: out of memory.");
    expect(notice).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("shows typing indicator when message status is streaming", () => {
    const streamingMsg: MessageViewType = {
      id: "a7",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [{ type: "text", text: "Thinking..." }],
      status: "streaming",
      created_at: "2026-10-21T18:00:07Z",
    };

    render(<MessageView message={streamingMsg} />);
    expect(screen.getByLabelText("Typing...")).toBeInTheDocument();
  });

  it("renders text and nothing for an approval part in an agent message", () => {
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
          approval_id: "apr_1",
          tool_call_id: "c8",
          preview: { kind: "command", title: "Run command", body: "rm -rf build" },
          status: "pending",
          note: null,
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:08Z",
    };

    const { container } = render(<MessageView message={agentMsg} />);
    expect(screen.getByText("I need your approval to run this.")).toBeInTheDocument();
    expect(screen.queryByText(/rm -rf build/)).not.toBeInTheDocument();
    expect(screen.queryByText("Run command")).not.toBeInTheDocument();
    // One rendered block: the text part. The approval part adds no wrapper.
    expect(container.querySelector(".space-y-2")?.children).toHaveLength(1);
  });
});
