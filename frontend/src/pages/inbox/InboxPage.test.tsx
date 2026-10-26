import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { FakeStream } from "@/pages/agents/session/testing";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import type { ApprovalView } from "./usePendingApprovals";

describe("InboxPage and badge", () => {
  let fakeStream: FakeStream;
  let getSpy: MockInstance | undefined;
  let postSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

  const sampleApprovals: ApprovalView[] = [
    {
      id: "appr_1",
      agent: "coder",
      tool: "Write",
      tool_call_id: "call_1",
      risk_class: "write_code",
      preview: {
        title: "Write src/main.py",
        kind: "diff",
        body: "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new",
      },
      status: "pending",
      note: null,
      session_id: "ses_1",
      turn_id: "trn_1",
      created_at: "2026-10-26T12:00:00Z",
      decided_at: null,
    },
    {
      id: "appr_2",
      agent: "orchestrator",
      tool: "Bash",
      tool_call_id: "call_2",
      risk_class: "run",
      preview: {
        title: "Run pytest",
        kind: "command",
        body: "pytest tests/",
      },
      status: "pending",
      note: null,
      session_id: "ses_2",
      turn_id: "trn_2",
      created_at: "2026-10-26T12:01:00Z",
      decided_at: null,
    },
  ];

  beforeEach(() => {
    fakeStream = new FakeStream();
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockImplementation(() => {
      setTimeout(() => {
        fakeStream.emitOpen();
      }, 0);
      return fakeStream;
    });
    stubProjects([project("prj_1", "Test Project", "2026-10-20T10:00:00Z")]);
  });

  afterEach(() => {
    getSpy?.mockRestore();
    postSpy?.mockRestore();
    openStreamSpy?.mockRestore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders empty state when there are no approvals", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: {
            id: "prj_1",
            name: "Test Project",
            status: "ok",
            source: "local",
            path: "/test",
            created_at: "2026-10-20T10:00:00Z",
            default_model: null,
            python_path: null,
            env_vars: [],
            status_detail: null,
            last_opened_at: "2026-10-20T10:00:00Z",
          },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByRole("heading", { name: "Inbox" })).toBeInTheDocument();
    expect(await screen.findByText("No pending approvals.")).toBeInTheDocument();

    const sidebar = within(screen.getByRole("complementary"));
    const inboxLink = sidebar.getByRole("link", { name: "Inbox" });
    expect(inboxLink).toBeInTheDocument();
    // No badge when 0
    expect(within(inboxLink).queryByText("0")).not.toBeInTheDocument();
  });

  it("renders pending approvals with agent names and ApprovalCard", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({
          data: sampleApprovals,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: {
            id: "prj_1",
            name: "Test Project",
            status: "ok",
            source: "local",
            path: "/test",
            created_at: "2026-10-20T10:00:00Z",
            default_model: null,
            python_path: null,
            env_vars: [],
            status_detail: null,
            last_opened_at: "2026-10-20T10:00:00Z",
          },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByRole("heading", { name: "Inbox" })).toBeInTheDocument();
    expect(await screen.findByText("2 pending")).toBeInTheDocument();
    expect(screen.getAllByText("From agent:")).toHaveLength(2);
    expect(screen.getByText("coder")).toBeInTheDocument();
    expect(screen.getByText("orchestrator")).toBeInTheDocument();
    expect(screen.getByText("Write src/main.py")).toBeInTheDocument();
    expect(screen.getByText("Run pytest")).toBeInTheDocument();

    // Check sidebar badge shows 2
    const sidebar = within(screen.getByRole("complementary"));
    const inboxLink = sidebar.getByRole("link", { name: /Inbox/ });
    await waitFor(() => {
      expect(within(inboxLink).getByText("2")).toBeInTheDocument();
    });
  });

  it("deciding an approval calls the decision endpoint", async () => {
    const user = userEvent.setup();

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({
          data: sampleApprovals,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: {
            id: "prj_1",
            name: "Test Project",
            status: "ok",
            source: "local",
            path: "/test",
            created_at: "2026-10-20T10:00:00Z",
            default_model: null,
            python_path: null,
            env_vars: [],
            status_detail: null,
            last_opened_at: "2026-10-20T10:00:00Z",
          },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    postSpy = vi.spyOn(api, "POST").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals/{approval_id}/decision") {
        return Promise.resolve({
          data: { ...sampleApprovals[1], status: "rejected", note: "Do not run" },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Run pytest")).toBeInTheDocument();

    const rejectNotes = screen.getAllByPlaceholderText("Optional note for rejection...");
    const rejectNote = rejectNotes[1];
    expect(rejectNote).toBeDefined();
    if (rejectNote) {
      await user.type(rejectNote, "Do not run");
    }

    const rejectButtons = screen.getAllByRole("button", { name: "Reject" });
    const rejectBtn = rejectButtons[1];
    expect(rejectBtn).toBeDefined();
    if (rejectBtn) {
      await user.click(rejectBtn);
    }

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      {
        params: {
          path: {
            project_id: "prj_1",
            approval_id: "appr_2",
          },
        },
        body: {
          approve: false,
          note: "Do not run",
        },
      },
    );
  });

  it("the badge counts and updates on an event", async () => {
    let currentApprovals = [...sampleApprovals];

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({
          data: currentApprovals,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: {
            id: "prj_1",
            name: "Test Project",
            status: "ok",
            source: "local",
            path: "/test",
            created_at: "2026-10-20T10:00:00Z",
            default_model: null,
            python_path: null,
            env_vars: [],
            status_detail: null,
            last_opened_at: "2026-10-20T10:00:00Z",
          },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    const sidebar = within(screen.getByRole("complementary"));
    const inboxLink = await sidebar.findByRole("link", { name: /Inbox/ });
    await waitFor(() => {
      expect(within(inboxLink).getByText("2")).toBeInTheDocument();
    });

    // Now one approval decided, remaining 1
    const firstApproval = sampleApprovals[0];
    expect(firstApproval).toBeDefined();
    currentApprovals = firstApproval ? [firstApproval] : [];
    act(() => {
      fakeStream.emit(
        "approval.decided",
        JSON.stringify({
          id: "evt_1",
          seq: 1,
          ts: "2026-10-26T12:02:00Z",
          source: "user",
          type: "approval.decided",
          subject_id: "appr_2",
          payload: { approval_id: "appr_2", status: "rejected" },
        }),
      );
    });

    await waitFor(() => {
      expect(within(inboxLink).getByText("1")).toBeInTheDocument();
    });

    // All approvals decided, remaining 0
    currentApprovals = [];
    act(() => {
      fakeStream.emit(
        "approval.decided",
        JSON.stringify({
          id: "evt_2",
          seq: 2,
          ts: "2026-10-26T12:03:00Z",
          source: "user",
          type: "approval.decided",
          subject_id: "appr_1",
          payload: { approval_id: "appr_1", status: "approved" },
        }),
      );
    });

    await waitFor(() => {
      expect(within(inboxLink).queryByText("1")).not.toBeInTheDocument();
      expect(within(inboxLink).queryByText("0")).not.toBeInTheDocument();
    });
  });

  it("shows error alert when approvals fetch fails", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.reject(new Error("Network connection lost"));
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: {
            id: "prj_1",
            name: "Test Project",
            status: "ok",
            source: "local",
            path: "/test",
            created_at: "2026-10-20T10:00:00Z",
            default_model: null,
            python_path: null,
            env_vars: [],
            status_detail: null,
            last_opened_at: "2026-10-20T10:00:00Z",
          },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Error loading approvals")).toBeInTheDocument();
    expect(screen.getByText("Network connection lost")).toBeInTheDocument();
  });
});
