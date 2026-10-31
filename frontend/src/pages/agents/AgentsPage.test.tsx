import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, type MockInstance } from "vitest";
import { renderPage } from "@/test/render";
import {
  type DefaultMockOverrides,
  FakeStream,
  setupAgentsOpenStreamMock,
  setupDefaultMocks as createDefaultMocks,
  testRoutes,
} from "./testing";

describe("AgentsPage", () => {
  let fakeStream: FakeStream;
  let getSpy: MockInstance | undefined;
  let postSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

  beforeEach(() => {
    fakeStream = new FakeStream();
    openStreamSpy = setupAgentsOpenStreamMock(fakeStream);
  });

  afterEach(() => {
    getSpy?.mockRestore();
    postSpy?.mockRestore();
    openStreamSpy?.mockRestore();
  });

  const setupDefaultMocks = (overrides?: DefaultMockOverrides) => {
    const mocks = createDefaultMocks(overrides);
    getSpy = mocks.getSpy;
    postSpy = mocks.postSpy;
  };

  it("keeps the message scroller between the header and composer", async () => {
    setupDefaultMocks({
      agents: [
        { name: "orchestrator", display_name: "Orchestrator", chat: true, settings: {} },
        { name: "analyst", display_name: "Analyst", chat: true, settings: {} },
      ],
    });
    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const switcher = await screen.findByRole("combobox", { name: "Select agent" });
    const input = screen.getByRole("textbox", { name: "Message Orchestrator" });
    const log = screen.getByRole("log", { name: "Messages" });
    const page = log.parentElement;
    expect(page).toHaveClass("flex", "h-full", "flex-col", "overflow-hidden");
    expect(log).toHaveClass("min-h-0", "flex-1", "overflow-y-auto");
    expect(page?.querySelectorAll(":scope > .overflow-y-auto")).toHaveLength(1);
    expect(log.previousElementSibling).toContainElement(switcher);
    expect(
      within(log.previousElementSibling as HTMLElement).getByRole("combobox", {
        name: "Select agent",
      }),
    ).toBe(switcher);
    expect(log.nextElementSibling).toContainElement(input);
  });

  it("switches agents with the labeled styled combobox", async () => {
    const user = userEvent.setup();
    setupDefaultMocks({
      agents: [
        { name: "orchestrator", display_name: "Orchestrator", chat: true, settings: {} },
        { name: "analyst", display_name: "Analyst", chat: true, settings: {} },
        { name: "worker", display_name: "Worker", chat: false, settings: {} },
      ],
    });
    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const switcher = await screen.findByRole("combobox", { name: "Select agent" });
    expect(switcher).toHaveAttribute("data-slot", "select-trigger");
    expect(screen.getByLabelText("Agent:")).toBe(switcher);
    switcher.focus();
    await user.keyboard("{ArrowDown}");
    expect(await screen.findByRole("option", { name: "Analyst" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Worker" })).not.toBeInTheDocument();
    await user.keyboard("{End}{Enter}");

    await screen.findByRole("textbox", { name: "Message Analyst" });
    await waitFor(() => {
      expect(getSpy).toHaveBeenCalledWith(
        "/api/projects/{project_id}/agents/{agent}/session",
        expect.objectContaining({ params: { path: { project_id: "prj_1", agent: "analyst" } } }),
      );
    });
  });

  it("renders a recorded turn with user message, closed tool step with file path, and markdown answer", async () => {
    const user = userEvent.setup();
    setupDefaultMocks({
      messages: [
        {
          id: "m_u1",
          turn_id: null,
          author_kind: "user",
          author_name: "user",
          from_agent: null,
          parts: [{ type: "text", text: "What does the README say?" }],
          status: "complete",
          created_at: "2026-10-21T18:00:00Z",
        },
        {
          id: "m_a1",
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
              content: "1\t# Demo Project\n2\tPipeline overview",
              is_error: false,
            },
            {
              type: "text",
              text: "The README outlines the **Demo Project** and its pipeline.",
            },
          ],
          status: "complete",
          created_at: "2026-10-21T18:00:05Z",
        },
      ],
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    expect(await screen.findByText("What does the README say?")).toBeInTheDocument();

    const toolSummary = screen.getByText("README.md");
    expect(toolSummary).toBeInTheDocument();
    expect(screen.getByText("Read")).toBeInTheDocument();

    expect(screen.queryByText(/Demo Project.*Pipeline overview/)).not.toBeInTheDocument();

    const toolButton = screen.getByRole("button", { name: /Read/ });
    await user.click(toolButton);

    expect(screen.getByText(/Pipeline overview/)).toBeInTheDocument();

    const markdownAnswer = screen.getByText(/The README outlines the/);
    expect(markdownAnswer).toBeInTheDocument();
    expect(screen.getByText("Demo Project")).toHaveProperty("tagName", "STRONG");
  });

  it("untrusted model output does not inject raw HTML and javascript: links are stripped", async () => {
    setupDefaultMocks({
      messages: [
        {
          id: "m_a1",
          turn_id: "trn_1",
          author_kind: "agent",
          author_name: "orchestrator",
          from_agent: null,
          parts: [
            {
              type: "text",
              text: "Here is raw html: <script>window.pwned = true;</script><img src='x' onerror='alert(1)' /> and a [malicious link](javascript:alert(1))",
            },
          ],
          status: "complete",
          created_at: "2026-10-21T18:00:00Z",
        },
      ],
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    expect(await screen.findByText(/<script>window\.pwned = true;<\/script>/)).toBeInTheDocument();

    expect(document.querySelector("script:not([src])")).toBeNull();
    expect(document.querySelector("img[onerror]")).toBeNull();

    const link = screen.queryByRole("link", { name: "malicious link" });
    if (link) {
      expect(link.getAttribute("href")).not.toMatch(/^javascript:/i);
    } else {
      expect(screen.getByText("malicious link")).toBeInTheDocument();
    }
  });

  it("marks queued, stopped and interrupted messages, and renders system notices", async () => {
    setupDefaultMocks({
      messages: [
        {
          id: "m1",
          turn_id: null,
          author_kind: "user",
          author_name: "user",
          from_agent: null,
          parts: [{ type: "text", text: "Queued question" }],
          status: "queued",
          created_at: "2026-10-21T18:00:00Z",
        },
        {
          id: "m2",
          turn_id: "t1",
          author_kind: "agent",
          author_name: "orchestrator",
          from_agent: null,
          parts: [{ type: "text", text: "Answer stopped" }],
          status: "stopped",
          created_at: "2026-10-21T18:00:01Z",
        },
        {
          id: "m3",
          turn_id: "t2",
          author_kind: "agent",
          author_name: "orchestrator",
          from_agent: null,
          parts: [{ type: "text", text: "Answer interrupted" }],
          status: "interrupted",
          created_at: "2026-10-21T18:00:02Z",
        },
        {
          id: "m4",
          turn_id: null,
          author_kind: "system",
          author_name: "system",
          from_agent: null,
          parts: [{ type: "text", text: "Turn failed: Model context length exceeded." }],
          status: "complete",
          created_at: "2026-10-21T18:00:03Z",
        },
      ],
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.getByText("Interrupted: the app stopped during this answer")).toBeInTheDocument();
    expect(screen.getByText("Turn failed: Model context length exceeded.")).toBeInTheDocument();
  });

  it("handles composer sending: Enter sends, Shift+Enter does not, Send disabled on empty", async () => {
    const user = userEvent.setup();
    setupDefaultMocks();

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const input = await screen.findByRole("textbox", { name: "Message Orchestrator" });
    const sendButton = screen.getByRole("button", { name: "Send" });

    expect(sendButton).toBeDisabled();

    await user.type(input, "   ");
    expect(sendButton).toBeDisabled();
    await user.clear(input);

    await user.type(input, "First line{Shift>}{Enter}{/Shift}Second line");
    expect(postSpy).not.toHaveBeenCalled();
    expect(sendButton).toBeEnabled();

    await user.type(input, "{Enter}");
    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/agents/{agent}/session/messages",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", agent: "orchestrator" } },
        body: { text: "First line\nSecond line" },
      }),
    );
  });

  it("while busy, shows Stop button that sends stop request", async () => {
    const user = userEvent.setup();
    setupDefaultMocks({ busy: true });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const stopButton = await screen.findByRole("button", { name: "Stop" });
    expect(stopButton).toBeInTheDocument();

    await user.click(stopButton);
    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/agents/{agent}/session/stop",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", agent: "orchestrator" } },
      }),
    );
  });

  it("renders the agent list with live status and updates session when an agent is selected", async () => {
    const user = userEvent.setup();
    setupDefaultMocks({
      agents: [
        { name: "orchestrator", display_name: "Orchestrator", chat: true, settings: {} },
        { name: "coder", display_name: "Coder", chat: true, settings: {} },
        {
          name: "alert_prioritization",
          display_name: "Alert Prioritization",
          chat: false,
          settings: {},
        },
      ],
      agentsStatus: [
        {
          agent: "orchestrator",
          display_name: "Orchestrator",
          state: "idle",
          task: null,
          open_prs: [],
          last_finished: null,
        },
        {
          agent: "coder",
          display_name: "Coder",
          state: "waiting_approval",
          task: null,
          open_prs: [],
          last_finished: null,
        },
        {
          agent: "alert_prioritization",
          display_name: "Alert Prioritization",
          state: "inactive",
          task: null,
          open_prs: [],
          last_finished: "starts in a later stage",
        },
      ],
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const agentsList = await screen.findByRole("complementary", { name: "Agents" });

    // Verify all agents and statuses appear in the list
    expect(within(agentsList).getByText("Orchestrator")).toBeInTheDocument();
    expect(within(agentsList).getByText("idle")).toBeInTheDocument();

    expect(within(agentsList).getByText("Coder")).toBeInTheDocument();
    expect(within(agentsList).getByText("waiting for approval")).toBeInTheDocument();
    expect(within(agentsList).getByText("Approval")).toBeInTheDocument();

    expect(within(agentsList).getByText("Alert Prioritization")).toBeInTheDocument();
    expect(within(agentsList).getByText("inactive")).toBeInTheDocument();
    expect(within(agentsList).getByText("(starts in a later stage)")).toBeInTheDocument();

    // Clicking Coder switches session
    const coderBtn = within(agentsList).getByRole("button", { name: /Coder/ });
    await user.click(coderBtn);

    await screen.findByRole("textbox", { name: "Message Coder" });
    await waitFor(() => {
      expect(getSpy).toHaveBeenCalledWith(
        "/api/projects/{project_id}/agents/{agent}/session",
        expect.objectContaining({ params: { path: { project_id: "prj_1", agent: "coder" } } }),
      );
    });
  });

  it("renders the bypass warning bar only when permission level is bypass", async () => {
    setupDefaultMocks({
      permissionLevel: "bypass",
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    expect(
      await screen.findByText(/Bypass mode: agents act without asking\./i),
    ).toBeInTheDocument();
  });

  it("does not render the bypass warning bar when permission level is ask", async () => {
    setupDefaultMocks({
      permissionLevel: "ask",
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    await screen.findByRole("textbox", { name: "Message Orchestrator" });
    expect(screen.queryByText(/Bypass mode: agents act without asking\./i)).not.toBeInTheDocument();
  });

  it("renders task bar above composer when session carries current_task", async () => {
    setupDefaultMocks({
      currentTask: {
        id: "tsk_01",
        title: "Add metrics tracking",
        branch: "task/metrics",
        status: "active",
        review_kind: "local",
        pr_number: null,
        pr_url: null,
      },
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    expect(await screen.findByText("Add metrics tracking")).toBeInTheDocument();
    expect(screen.getByText("task/metrics")).toBeInTheDocument();
    expect(screen.getByText("local review")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard task" })).toBeInTheDocument();

    const composer = screen.getByRole("textbox", { name: "Message Orchestrator" });
    expect(composer).toBeInTheDocument();
  });

  it("Compact now sends POST to compact endpoint", async () => {
    const user = userEvent.setup();
    setupDefaultMocks();
    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const compactButton = await screen.findByRole("button", { name: "Compact now" });
    await user.click(compactButton);

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/agents/{agent}/session/compact",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", agent: "orchestrator" } },
      }),
    );
  });

  it("Clear context opens confirmation dialog and confirming sends POST to clear endpoint", async () => {
    const user = userEvent.setup();
    setupDefaultMocks();
    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const clearButton = await screen.findByRole("button", { name: "Clear context" });
    await user.click(clearButton);

    expect(
      screen.getByText(
        "Clear conversation context? The agent will forget earlier messages and start fresh. Earlier messages remain visible in the history. An agent working on an active task or incident may need to re-read repository context.",
      ),
    ).toBeInTheDocument();

    const dialogClearButton = screen.getByRole("button", { name: "Confirm clear" });
    await user.click(dialogClearButton);

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/agents/{agent}/session/clear",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", agent: "orchestrator" } },
      }),
    );
  });

  it("shows server's error message on 409 session_busy", async () => {
    const user = userEvent.setup();
    setupDefaultMocks();
    postSpy?.mockImplementation((path: string) => {
      if (path.includes("/compact") || path.includes("/clear")) {
        return Promise.resolve({
          data: null,
          error: {
            code: "session_busy",
            message: "The agent is busy. Try again when it finishes.",
          },
          response: new Response(
            JSON.stringify({
              code: "session_busy",
              message: "The agent is busy. Try again when it finishes.",
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          ),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const compactButton = await screen.findByRole("button", { name: "Compact now" });
    await user.click(compactButton);

    expect(
      await screen.findByText("The agent is busy. Try again when it finishes."),
    ).toBeInTheDocument();
  });
});
