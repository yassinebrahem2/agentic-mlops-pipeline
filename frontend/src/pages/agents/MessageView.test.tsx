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

  it("shows 'Failed' and warning icon on collapsed error tool step, neither on success, and 'Running' when running", () => {
    const errorMsg: MessageViewType = {
      id: "a-error",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "tool_call",
          id: "c-err",
          name: "Read",
          input: { file_path: "missing.txt" },
        },
        {
          type: "tool_result",
          tool_call_id: "c-err",
          content: "File not found",
          is_error: true,
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:03Z",
    };

    const { rerender } = render(<MessageView message={errorMsg} />);

    // 1. A step with an error result shows "Failed" and the warning icon without being expanded.
    const failedButton = screen.getByRole("button", { name: /Read missing\.txt/i });
    expect(failedButton).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("File not found")).not.toBeInTheDocument();

    const failedBadge = screen.getByText("Failed");
    expect(failedBadge).toBeInTheDocument();
    expect(failedBadge.closest("[data-slot='badge']")).toHaveAttribute(
      "data-variant",
      "destructive",
    );
    const warningIcon = failedBadge.querySelector("svg");
    expect(warningIcon).toBeInTheDocument();
    expect(warningIcon).toHaveAttribute("aria-hidden", "true");
    expect(warningIcon).toHaveClass("text-destructive");

    // 2. A successful step shows neither.
    const successMsg: MessageViewType = {
      id: "a-success",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "tool_call",
          id: "c-ok",
          name: "Read",
          input: { file_path: "README.md" },
        },
        {
          type: "tool_result",
          tool_call_id: "c-ok",
          content: "File contents",
          is_error: false,
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:04Z",
    };
    rerender(<MessageView message={successMsg} />);
    const successButton = screen.getByRole("button", { name: /Read README\.md/i });
    expect(successButton).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(screen.queryByText("Running")).not.toBeInTheDocument();
    expect(
      successButton.querySelector("svg[aria-hidden='true'].text-destructive"),
    ).not.toBeInTheDocument();

    // 3. A step with no result still shows "Running".
    const runningMsg: MessageViewType = {
      id: "a-running",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "tool_call",
          id: "c-run",
          name: "Grep",
          input: { pattern: "TODO" },
        },
      ],
      status: "streaming",
      created_at: "2026-10-21T18:00:05Z",
    };
    rerender(<MessageView message={runningMsg} />);
    const runningButton = screen.getByRole("button", { name: /Grep TODO/i });
    expect(runningButton).toHaveAttribute("aria-expanded", "false");
    const runningBadge = screen.getByText("Running");
    expect(runningBadge).toBeInTheDocument();
    expect(runningBadge.closest("[data-slot='badge']")).toHaveAttribute(
      "data-variant",
      "secondary",
    );
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
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

  it("renders GFM tables with header, body cells, and border classes", () => {
    const tableMsg: MessageViewType = {
      id: "a-tbl",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "text",
          text: "| Col 1 | Col 2 |\n| --- | --- |\n| Val 1 | Val 2 |",
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:08Z",
    };

    const { container } = render(<MessageView message={tableMsg} />);
    const table = container.querySelector("table");
    expect(table).toBeInTheDocument();
    expect(table).toHaveClass("my-4", "border", "border-border");
    expect(table).not.toHaveAttribute("node");

    const th = screen.getByRole("columnheader", { name: "Col 1" });
    expect(th).toBeInTheDocument();
    expect(th).toHaveClass("border", "border-border", "font-semibold");
    expect(th).not.toHaveAttribute("node");

    const td = screen.getByRole("cell", { name: "Val 1" });
    expect(td).toBeInTheDocument();
    expect(td).toHaveClass("border", "border-border");
    expect(td).not.toHaveAttribute("node");
  });

  it("renders links and pre blocks with proper styling classes and no node attribute", () => {
    const textMsg: MessageViewType = {
      id: "a-fmt",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "text",
          text: "Visit [site](https://example.com) and run:\n\n```\necho hi\n```",
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:09Z",
    };

    const { container } = render(<MessageView message={textMsg} />);
    const link = screen.getByRole("link", { name: "site" });
    expect(link).toHaveClass("text-primary", "underline");
    expect(link).not.toHaveAttribute("node");

    const pre = container.querySelector("pre");
    expect(pre).toBeInTheDocument();
    expect(pre).toHaveClass("my-3", "overflow-x-auto", "rounded-lg", "bg-muted/50", "p-3");
    expect(pre).not.toHaveAttribute("node");
  });

  it("renders 'From <author_name>' when user message has from_agent from another agent", () => {
    const userMsg: MessageViewType = {
      id: "u_from_agent",
      turn_id: null,
      author_kind: "user",
      author_name: "Reviewer",
      from_agent: "reviewer",
      parts: [{ type: "text", text: "Please address the PR comments." }],
      status: "complete",
      created_at: "2026-10-21T18:00:00Z",
    };

    render(<MessageView message={userMsg} sessionAgent="orchestrator" />);
    expect(screen.getByText("From Reviewer")).toBeInTheDocument();
    expect(screen.getByText("Please address the PR comments.")).toBeInTheDocument();
  });

  it("renders author_name alone when user message has from_agent matching sessionAgent", () => {
    const userMsg: MessageViewType = {
      id: "u_wakeup",
      turn_id: null,
      author_kind: "user",
      author_name: "Orchestrator",
      from_agent: "orchestrator",
      parts: [{ type: "text", text: "Wake up: check task status." }],
      status: "complete",
      created_at: "2026-10-21T18:00:00Z",
    };

    render(<MessageView message={userMsg} sessionAgent="orchestrator" />);
    expect(screen.getByText("Orchestrator")).toBeInTheDocument();
    expect(screen.queryByText("From Orchestrator")).not.toBeInTheDocument();
    expect(screen.getByText("Wake up: check task status.")).toBeInTheDocument();
  });

  it("renders no label when user message has no from_agent", () => {
    const userMsg: MessageViewType = {
      id: "u_human",
      turn_id: null,
      author_kind: "user",
      author_name: "user",
      from_agent: null,
      parts: [{ type: "text", text: "Hello from human user!" }],
      status: "complete",
      created_at: "2026-10-21T18:00:00Z",
    };

    render(<MessageView message={userMsg} sessionAgent="orchestrator" />);
    expect(screen.queryByText("user")).not.toBeInTheDocument();
    expect(screen.queryByText("From user")).not.toBeInTheDocument();
    expect(screen.getByText("Hello from human user!")).toBeInTheDocument();
  });

  it("renders headings, lists with nested bullet lists, blockquotes, inline code, hr, and paragraphs with styling and list markers", () => {
    const markdownMsg: MessageViewType = {
      id: "a-md",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "text",
          text: [
            "# Heading One",
            "",
            "First paragraph with `inline code` inside it.",
            "",
            "Second paragraph before a divider.",
            "",
            "---",
            "",
            "> Blockquote note",
            "",
            "1. First item",
            "   - Nested bullet item",
            "2. Second item",
            "",
            "```",
            "block of code",
            "```",
          ].join("\n"),
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:10Z",
    };

    const { container } = render(<MessageView message={markdownMsg} />);

    // Body text container uses text-base leading-relaxed text-foreground
    const bodyContainer = container.querySelector(".text-base.leading-relaxed.text-foreground");
    expect(bodyContainer).toBeInTheDocument();

    // Heading
    const heading = screen.getByRole("heading", { level: 1, name: "Heading One" });
    expect(heading).toBeInTheDocument();
    expect(heading).toHaveClass("text-2xl", "font-bold");
    expect(heading).not.toHaveAttribute("node");

    // Paragraphs
    const paragraphs = container.querySelectorAll("p");
    expect(paragraphs.length).toBeGreaterThanOrEqual(2);
    expect(paragraphs[0]).toHaveClass("my-3.5", "leading-relaxed");
    expect(paragraphs[0]).not.toHaveAttribute("node");

    // Inline code
    const inlineCode = screen.getByText("inline code");
    expect(inlineCode).toBeInTheDocument();
    expect(inlineCode.tagName.toLowerCase()).toBe("code");
    expect(inlineCode).toHaveClass(
      "bg-muted",
      "px-1.5",
      "py-0.5",
      "rounded",
      "font-mono",
      "text-xs",
    );
    expect(inlineCode).not.toHaveAttribute("node");

    // Block code (inside pre) does not get inline code styling
    const preCode = container.querySelector("pre code");
    expect(preCode).toBeInTheDocument();
    expect(preCode).not.toHaveClass("bg-muted", "px-1.5", "py-0.5");
    expect(preCode).not.toHaveAttribute("node");

    // Horizontal rule
    const hr = container.querySelector("hr");
    expect(hr).toBeInTheDocument();
    expect(hr).toHaveClass("border-t", "border-border");
    expect(hr).not.toHaveAttribute("node");

    // Blockquote
    const blockquote = container.querySelector("blockquote");
    expect(blockquote).toBeInTheDocument();
    expect(blockquote).toHaveClass("my-3.5", "border-l-2", "border-border", "pl-4", "italic");
    expect(blockquote).not.toHaveAttribute("node");

    // Ordered list and nested bullet list
    const ol = container.querySelector("ol");
    expect(ol).toBeInTheDocument();
    expect(ol).toHaveClass("my-3", "list-decimal", "pl-6", "space-y-1.5");
    expect(ol).not.toHaveAttribute("node");

    const ul = container.querySelector("ol ul");
    expect(ul).toBeInTheDocument();
    expect(ul).toHaveClass("my-3", "list-disc", "pl-6", "space-y-1.5");
    expect(ul).not.toHaveAttribute("node");

    const listItems = container.querySelectorAll("li");
    expect(listItems.length).toBe(3);
    for (const li of listItems) {
      expect(li).toHaveClass("leading-relaxed");
      expect(li).not.toHaveAttribute("node");
    }
  });

  it("renders headings h2 through h6 with appropriate typography classes", () => {
    const headingsMsg: MessageViewType = {
      id: "a-headings",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [
        {
          type: "text",
          text: [
            "## Heading Two",
            "### Heading Three",
            "#### Heading Four",
            "##### Heading Five",
            "###### Heading Six",
          ].join("\n\n"),
        },
      ],
      status: "complete",
      created_at: "2026-10-21T18:00:11Z",
    };

    render(<MessageView message={headingsMsg} />);
    const h2 = screen.getByRole("heading", { level: 2, name: "Heading Two" });
    const h3 = screen.getByRole("heading", { level: 3, name: "Heading Three" });
    const h4 = screen.getByRole("heading", { level: 4, name: "Heading Four" });
    const h5 = screen.getByRole("heading", { level: 5, name: "Heading Five" });
    const h6 = screen.getByRole("heading", { level: 6, name: "Heading Six" });

    expect(h2).toHaveClass("text-xl", "font-semibold");
    expect(h3).toHaveClass("text-lg", "font-semibold");
    expect(h4).toHaveClass("text-base", "font-semibold");
    expect(h5).toHaveClass("text-sm", "font-semibold");
    expect(h6).toHaveClass("text-xs", "font-semibold");

    for (const h of [h2, h3, h4, h5, h6]) {
      expect(h).not.toHaveAttribute("node");
    }
  });
});
