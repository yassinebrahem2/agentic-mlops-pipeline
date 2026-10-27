import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import { renderPage } from "@/test/render";
import {
  type DefaultMockOverrides,
  FakeStream,
  setupAgentsOpenStreamMock,
  setupDefaultMocks as createDefaultMocks,
  STREAM_CONNECTING,
  testRoutes,
} from "./testing";

describe("AgentsPageStatus", () => {
  let fakeStream: FakeStream;
  let getSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

  beforeEach(() => {
    fakeStream = new FakeStream();
    openStreamSpy = setupAgentsOpenStreamMock(fakeStream);
  });

  afterEach(() => {
    getSpy?.mockRestore();
    openStreamSpy?.mockRestore();
  });

  const setupDefaultMocks = (overrides?: DefaultMockOverrides) => {
    const mocks = createDefaultMocks(overrides);
    getSpy = mocks.getSpy;
  };

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

    await waitFor(() => {
      expect(fakeStream.onerror).toBeDefined();
    });

    act(() => {
      fakeStream.readyState = STREAM_CONNECTING;
      fakeStream.onerror?.(new Event("error"));
    });

    expect(await screen.findByText("Reconnecting...")).toBeInTheDocument();
  });

  it("shows error, Retry button and link to Projects when stream is closed with 404", async () => {
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
      if (path === "/api/projects/{project_id}/agents/{agent}/session") {
        return Promise.reject(new ApiError("project_not_found", "Project prj_1 not found.", 404));
      }
      return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Project prj_1 not found.")).toBeInTheDocument();

    const retryButton = within(alert).getByRole("button", { name: "Retry" });
    expect(retryButton).toBeInTheDocument();

    const projectsLink = within(alert).getByRole("link", { name: "Projects" });
    expect(projectsLink).toHaveAttribute("href", "/");
  });

  it("shows 'No chat agents found.' when the project has no chat agents", async () => {
    setupDefaultMocks({
      agents: [
        {
          name: "worker",
          display_name: "Background Worker",
          chat: false,
          settings: { model_problem: null },
        },
      ],
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    expect(await screen.findByText("No chat agents found.")).toBeInTheDocument();
  });

  it("shows error banner and Retry button when agents query fails, and retrying re-fetches", async () => {
    const user = userEvent.setup();
    let agentsCallCount = 0;

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
        agentsCallCount += 1;
        if (agentsCallCount === 1) {
          return Promise.reject(new ApiError("load_agents_failed", "Failed to fetch agents", 500));
        }
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
      if (path === "/api/projects/{project_id}/agents/{agent}/session") {
        return Promise.resolve({
          data: {
            id: "s1",
            agent: "orchestrator",
            busy: false,
            messages: [],
            last_seq: 10,
          },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Failed to load agents")).toBeInTheDocument();
    expect(within(alert).getByText("Failed to fetch agents")).toBeInTheDocument();

    const retryButton = within(alert).getByRole("button", { name: "Retry" });
    await user.click(retryButton);

    expect(
      await screen.findByRole("textbox", { name: "Message Orchestrator" }),
    ).toBeInTheDocument();
  });

  it("hides a task the session snapshot says has ended, even while the status list shows it", async () => {
    setupDefaultMocks({
      currentTask: null,
      agentsStatus: [
        {
          agent: "orchestrator",
          display_name: "Orchestrator",
          state: "idle",
          open_prs: [],
          last_finished: null,
          task: {
            id: "tsk_from_status",
            title: "Task from status",
            branch: "task/status-branch",
            status: "active",
            review_kind: "local",
            pr_number: null,
            pr_url: null,
          },
        },
      ],
    });

    renderPage("/projects/prj_1/agents", { routes: testRoutes });

    // The status list (with its task) has loaded, and so has the snapshot: its stream is open.
    const agentsList = await screen.findByRole("complementary", { name: "Agents" });
    expect(within(agentsList).getByText("idle")).toBeInTheDocument();
    await waitFor(() => {
      expect(fakeStream.listeners.has("text.delta")).toBe(true);
    });

    // The snapshot has no current task, so the bar stays hidden.
    expect(screen.queryByRole("region", { name: "Current task" })).not.toBeInTheDocument();
    expect(screen.queryByText("Task from status")).not.toBeInTheDocument();
  });
});
