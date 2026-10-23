import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { PageRoute } from "../../app/routes";
import { ApiError, api } from "../../lib/api";
import { renderPage } from "../../test/render";
import { route } from "./route";
import * as streamModule from "./session/stream";
import { STREAM_CLOSED, STREAM_CONNECTING, STREAM_OPEN, type StreamLike } from "./session/stream";

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
    fakeStream = new FakeStream();
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockReturnValue(fakeStream);
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
      if (path.startsWith("/api/projects/prj_1/agents/orchestrator/session")) {
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
              content: "1\t# Demo Project\n2\tMLOps pipeline.",
              is_error: false,
            },
            {
              type: "text",
              text: "It says **Demo Project**.",
            },
          ],
          status: "complete",
          created_at: "2026-10-21T18:00:01Z",
        },
      ],
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    expect(await screen.findByText("What does the README say?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Read README\.md/i })).toBeInTheDocument();
    expect(screen.getByText("Demo Project")).toBeInTheDocument();

    // Open tool step reveals file content
    await user.click(screen.getByRole("button", { name: /Read README\.md/i }));
    expect(screen.getByText(/MLOps pipeline\./)).toBeInTheDocument();
  });

  it("sending: Enter sends, Shift+Enter does not, empty text cannot be sent", async () => {
    const user = userEvent.setup();
    setupDefaultMocks();

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const input = await screen.findByRole("textbox", { name: "Message Orchestrator" });
    const sendButton = screen.getByRole("button", { name: "Send" });

    // Empty text: Send button disabled
    expect(sendButton).toBeDisabled();

    // Whitespace only: Send button still disabled
    await user.type(input, "   ");
    expect(sendButton).toBeDisabled();
    await user.clear(input);

    // Type text and Shift+Enter -> does not send
    await user.type(input, "First line{Shift>}{Enter}{/Shift}Second line");
    expect(postSpy).not.toHaveBeenCalled();
    expect(sendButton).toBeEnabled();

    // Enter -> sends
    await user.type(input, "{Enter}");
    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/prj_1/agents/orchestrator/session/messages",
      expect.objectContaining({
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
      "/api/projects/prj_1/agents/orchestrator/session/stop",
      expect.objectContaining({
        body: undefined,
      }),
    );
  });

  it("shows model_problem warning banner with links to /providers and project settings", async () => {
    setupDefaultMocks({
      modelProblem: "No model configured for Orchestrator.",
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("No model configured for Orchestrator.")).toBeInTheDocument();

    const providersLink = within(alert).getByRole("link", { name: "AI providers" });
    expect(providersLink).toHaveAttribute("href", "/providers");

    const settingsLink = within(alert).getByRole("link", { name: "Project settings" });
    expect(settingsLink).toHaveAttribute("href", "/projects/prj_1/settings");
  });

  it("shows project status banner when project is not ok", async () => {
    setupDefaultMocks({
      projectStatus: "missing",
      statusDetail: "/path/to/project does not exist",
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    expect(await screen.findByText("Folder not found")).toBeInTheDocument();
    expect(screen.getByText("/path/to/project does not exist")).toBeInTheDocument();
  });

  it("shows 'Reconnecting...' note while stream is reconnecting", async () => {
    setupDefaultMocks();

    renderPage("/projects/prj_1/agents", { routes: testRoutes });
    await screen.findByRole("textbox", { name: "Message Orchestrator" });

    // Trigger reconnecting on fake stream
    act(() => {
      fakeStream.readyState = STREAM_CONNECTING;
      fakeStream.onerror?.(new Event("error"));
    });

    expect(await screen.findByText("Reconnecting...")).toBeInTheDocument();
  });

  it("shows error and link to Projects when stream is closed with 404", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: {
            id: "prj_1",
            name: "Test Project",
            status: "ok",
            status_detail: null,
            source: "local",
            path: "/test",
            last_opened: "2026-10-21T00:00:00Z",
          },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/agents") {
        return Promise.resolve({
          data: [
            {
              name: "orchestrator",
              display_name: "Orchestrator",
              chat: true,
              settings: { model_problem: null },
            },
          ],
          response: new Response(null, { status: 200 }),
        });
      }
      if (path.startsWith("/api/projects/prj_1/agents/orchestrator/session")) {
        return Promise.reject(new ApiError("project_not_found", "Project prj_1 not found.", 404));
      }
      return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Project prj_1 not found.")).toBeInTheDocument();

    const projectsLink = within(alert).getByRole("link", { name: "Projects" });
    expect(projectsLink).toHaveAttribute("href", "/");
  });
});
