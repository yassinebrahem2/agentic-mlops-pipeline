import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { PageRoute } from "@/app/routes";
import { api } from "@/lib/api";
import { queryClient } from "@/lib/query";
import { renderPage } from "@/test/render";
import { route } from "./route";
import * as streamModule from "./session/stream";
import { STREAM_CLOSED, STREAM_OPEN, type StreamLike } from "./session/stream";

const testRoutes: PageRoute[] = [
  route,
  { path: "/", element: <div>Projects</div> },
  { path: "/providers", element: <div>AI providers</div> },
  { path: "/projects/:projectId/settings", element: <div>Project settings</div> },
];

class FakeStream implements StreamLike {
  readyState = STREAM_OPEN;
  onerror: ((event: Event) => void) | null = null;
  listeners = new Map<string, (event: Event) => void>();

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    if (typeof listener === "function") {
      this.listeners.set(type, listener);
    }
  }

  close = vi.fn(() => {
    this.readyState = STREAM_CLOSED;
  });

  emitOpen(): void {
    this.listeners.get("open")?.(new Event("open"));
  }

  emit(type: string, data: unknown, seq: number): void {
    this.listeners.get(type)?.({
      type,
      data: JSON.stringify(data),
      lastEventId: String(seq),
    } as MessageEvent);
  }
}

describe("AgentsPage", () => {
  let fakeStream: FakeStream;
  let getSpy: MockInstance | undefined;
  let postSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

  beforeEach(() => {
    queryClient.clear();
    fakeStream = new FakeStream();
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockImplementation(() => {
      setTimeout(() => {
        fakeStream.emitOpen();
      }, 0);
      return fakeStream;
    });
  });

  afterEach(() => {
    getSpy?.mockRestore();
    postSpy?.mockRestore();
    openStreamSpy?.mockRestore();
  });

  const setupDefaultMocks = (overrides?: {
    projectStatus?: "ok" | "missing" | "empty" | "clone_failed";
    statusDetail?: string | null;
    modelProblem?: string | null;
    busy?: boolean;
    messages?: unknown[];
    agents?: unknown[];
  }) => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: {
            id: "prj_1",
            name: "Test Project",
            status: overrides?.projectStatus ?? "ok",
            status_detail: overrides?.statusDetail ?? null,
            source: "local",
            path: "/test",
            last_opened: "2026-10-21T00:00:00Z",
          },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/agents") {
        return Promise.resolve({
          data: overrides?.agents ?? [
            {
              name: "orchestrator",
              display_name: "Orchestrator",
              chat: true,
              settings: {
                model_problem: overrides?.modelProblem ?? null,
              },
            },
          ],
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/agents/{agent}/session") {
        return Promise.resolve({
          data: {
            id: "s1",
            agent: "orchestrator",
            busy: overrides?.busy ?? false,
            messages: overrides?.messages ?? [],
            last_seq: 10,
          },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
    });

    postSpy = vi.spyOn(api, "POST").mockImplementation(() => {
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });
  };

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
});
