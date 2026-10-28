import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { STREAM_CLOSED, STREAM_OPEN, type StreamLike } from "@/pages/agents/session/stream";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import {
  CARD_FOR_KIND,
  CARD_FOR_PROPOSAL,
  type ApprovalCardComponent,
} from "@/pages/agents/ApprovalCard";
import type { ApprovalView, GitHubItem, LocalReview } from "./usePendingApprovals";

class TestFakeStream implements StreamLike {
  url: string = "";
  readyState = STREAM_OPEN;
  onerror: ((event: Event) => void) | null = null;
  listeners = new Map<string, Set<(event: Event) => void>>();

  addEventListener(type: string, listener: (event: Event) => void): void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(listener);
  }

  removeEventListener(type: string, listener: (event: Event) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  close = vi.fn(() => {
    this.readyState = STREAM_CLOSED;
  });

  emitOpen(): void {
    this.listeners.get("open")?.forEach((fn) => {
      fn(new Event("open"));
    });
  }

  emit(type: string, data: unknown, seq?: number): void {
    const event = {
      type,
      data: typeof data === "string" ? data : JSON.stringify(data),
      lastEventId: seq !== undefined ? String(seq) : undefined,
    } as unknown as MessageEvent;
    this.listeners.get(type)?.forEach((fn) => {
      fn(event);
    });
  }
}

describe("InboxPage and badge", () => {
  let fakeStream: TestFakeStream;
  let getSpy: MockInstance | undefined;
  let postSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

  const sampleApprovals: ApprovalView[] = [
    {
      id: "appr_1",
      agent: "coder",
      kind: "tool",
      tool: "Write",
      tool_call_id: "call_1",
      risk_class: "write_code",
      step: null,
      proposal_kind: null,
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
      kind: "tool",
      tool: "Bash",
      tool_call_id: "call_2",
      risk_class: "run",
      step: null,
      proposal_kind: null,
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

  const sampleReviews: LocalReview[] = [
    {
      agent: "coder",
      task: {
        id: "tsk_1",
        agent: "coder",
        title: "Add user profile endpoint",
        branch: "agent/coder/task-1",
        status: "in_review",
        review_kind: "local",
        pr_number: null,
        pr_url: null,
      },
      files_changed: ["src/users/profile.py", "tests/test_profile.py"],
      diff: "--- a/src/users/profile.py\n+++ b/src/users/profile.py\n@@ -0,0 +1,5 @@\n+def get_profile():\n+    pass\n",
    },
  ];

  const sampleActivity: GitHubItem[] = [
    {
      kind: "pr",
      number: 42,
      title: "feat: add analytics pipeline",
      state: "open",
      agent: "orchestrator",
      url: "https://github.com/org/repo/pull/42",
      created_at: "2026-10-26T12:30:00Z",
    },
    {
      kind: "issue",
      number: 7,
      title: "bug: fix memory leak",
      state: "open",
      agent: "debugger",
      url: "https://github.com/org/repo/issues/7",
      created_at: "2026-10-26T11:00:00Z",
    },
  ];

  const projectStub = {
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
  };

  beforeEach(() => {
    fakeStream = new TestFakeStream();
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

  it("renders empty state when there are no approvals, reviews, or activity", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByRole("heading", { name: "Inbox" })).toBeInTheDocument();
    expect(await screen.findByText("No pending approvals.")).toBeInTheDocument();
    expect(await screen.findByText("No changes to merge.")).toBeInTheDocument();
    expect(await screen.findByText("No pull requests or issues.")).toBeInTheDocument();

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
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
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

  it("renders changes to merge with agent, task title, files changed, and diff", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: sampleReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Changes to merge")).toBeInTheDocument();
    expect(await screen.findByText("1 waiting")).toBeInTheDocument();
    expect(screen.getByText("Add user profile endpoint")).toBeInTheDocument();
    expect(screen.getByText("These files in your folder will change:")).toBeInTheDocument();
    expect(screen.getByText("src/users/profile.py")).toBeInTheDocument();
    expect(screen.getByText("tests/test_profile.py")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Diff view" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();

    // Sidebar badge counts 1 from review
    const sidebar = within(screen.getByRole("complementary"));
    const inboxLink = sidebar.getByRole("link", { name: /Inbox/ });
    await waitFor(() => {
      expect(within(inboxLink).getByText("1")).toBeInTheDocument();
    });
  });

  it("renders pull requests and issues activity with external links", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({
          data: sampleActivity,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Pull requests and issues")).toBeInTheDocument();
    expect(await screen.findByText("2 total")).toBeInTheDocument();

    const prItem = await screen.findByTestId("activity-item-pr-42");
    expect(within(prItem).getByText("#42 feat: add analytics pipeline")).toBeInTheDocument();
    expect(within(prItem).getByText("orchestrator")).toBeInTheDocument();
    expect(within(prItem).getByText("open")).toBeInTheDocument();
    const prLink = within(prItem).getByRole("link");
    expect(prLink).toHaveAttribute("href", "https://github.com/org/repo/pull/42");
    expect(prLink).toHaveAttribute("target", "_blank");

    const issueItem = screen.getByTestId("activity-item-issue-7");
    expect(within(issueItem).getByText("#7 bug: fix memory leak")).toBeInTheDocument();
    expect(within(issueItem).getByText("debugger")).toBeInTheDocument();
    expect(within(issueItem).getByText("open")).toBeInTheDocument();
    const issueLink = within(issueItem).getByRole("link");
    expect(issueLink).toHaveAttribute("href", "https://github.com/org/repo/issues/7");
    expect(issueLink).toHaveAttribute("target", "_blank");
  });

  it("approving a local review calls the decision endpoint", async () => {
    const user = userEvent.setup();

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: sampleReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    const firstReview = sampleReviews[0];
    postSpy = vi.spyOn(api, "POST").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/reviews/{task_id}/decision") {
        return Promise.resolve({
          data: { ...(firstReview ? firstReview.task : {}), status: "merged" },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Add user profile endpoint")).toBeInTheDocument();
    const approveBtn = screen.getByRole("button", { name: "Approve" });
    await user.click(approveBtn);

    expect(postSpy).toHaveBeenCalledWith("/api/projects/{project_id}/reviews/{task_id}/decision", {
      params: {
        path: {
          project_id: "prj_1",
          task_id: "tsk_1",
        },
      },
      body: {
        approve: true,
        note: null,
      },
    });
  });

  it("rejecting a local review with a note calls the decision endpoint", async () => {
    const user = userEvent.setup();

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: sampleReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    const firstReview = sampleReviews[0];
    postSpy = vi.spyOn(api, "POST").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/reviews/{task_id}/decision") {
        return Promise.resolve({
          data: { ...(firstReview ? firstReview.task : {}), status: "closed" },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Add user profile endpoint")).toBeInTheDocument();

    const noteInput = screen.getByPlaceholderText("Optional note for rejection...");
    await user.type(noteInput, "Please add more unit tests");

    const rejectBtn = screen.getByRole("button", { name: "Reject" });
    await user.click(rejectBtn);

    expect(postSpy).toHaveBeenCalledWith("/api/projects/{project_id}/reviews/{task_id}/decision", {
      params: {
        path: {
          project_id: "prj_1",
          task_id: "tsk_1",
        },
      },
      body: {
        approve: false,
        note: "Please add more unit tests",
      },
    });
  });

  it("shows 409 conflict error message on review decision", async () => {
    const user = userEvent.setup();

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: sampleReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    postSpy = vi.spyOn(api, "POST").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/reviews/{task_id}/decision") {
        return Promise.resolve({
          error: {
            code: "conflict",
            message: "Cannot merge: conflicting changes in src/users/profile.py",
          },
          response: new Response(null, { status: 409 }),
        });
      }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Add user profile endpoint")).toBeInTheDocument();
    const approveBtn = screen.getByRole("button", { name: "Approve" });
    await user.click(approveBtn);

    expect(
      await screen.findByText("Cannot merge: conflicting changes in src/users/profile.py"),
    ).toBeInTheDocument();
  });

  it("the badge counts pending approvals plus local reviews and updates on an event", async () => {
    let currentApprovals = [...sampleApprovals];
    let currentReviews = [...sampleReviews];

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({
          data: currentApprovals,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: currentReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    const sidebar = within(screen.getByRole("complementary"));
    const inboxLink = await sidebar.findByRole("link", { name: /Inbox/ });
    // 2 approvals + 1 review = 3
    await waitFor(() => {
      expect(within(inboxLink).getByText("3")).toBeInTheDocument();
    });

    // Approval decided: 1 approval left, 1 review left = 2
    const firstApproval = sampleApprovals[0];
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

    await waitFor(
      () => {
        expect(within(inboxLink).getByText("2")).toBeInTheDocument();
      },
      { timeout: 3000 },
    );

    // Review ended: 1 approval left, 0 reviews left = 1
    currentReviews = [];
    act(() => {
      fakeStream.emit(
        "task.ended",
        JSON.stringify({
          id: "evt_2",
          seq: 2,
          ts: "2026-10-26T12:03:00Z",
          source: "user",
          type: "task.ended",
          subject_id: "tsk_1",
          payload: { task_id: "tsk_1", status: "merged" },
        }),
      );
    });

    await waitFor(
      () => {
        expect(within(inboxLink).getByText("1")).toBeInTheDocument();
      },
      { timeout: 3000 },
    );

    // All cleared: 0 left = no badge
    currentApprovals = [];
    act(() => {
      fakeStream.emit(
        "approval.decided",
        JSON.stringify({
          id: "evt_3",
          seq: 3,
          ts: "2026-10-26T12:04:00Z",
          source: "user",
          type: "approval.decided",
          subject_id: "appr_1",
          payload: { approval_id: "appr_1", status: "approved" },
        }),
      );
    });

    await waitFor(
      () => {
        expect(within(inboxLink).queryByText("1")).not.toBeInTheDocument();
        expect(within(inboxLink).queryByText("0")).not.toBeInTheDocument();
      },
      { timeout: 3000 },
    );
  });

  it("shows error alert when approvals fetch fails", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.reject(new ApiError("internal", "Network connection lost", 500));
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Error loading approvals")).toBeInTheDocument();
    expect(screen.getByText("Network connection lost")).toBeInTheDocument();
  });

  it("shows error alert when reviews fetch fails", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.reject(new ApiError("internal", "Database disk full", 500));
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Error loading local reviews")).toBeInTheDocument();
    expect(screen.getByText("Database disk full")).toBeInTheDocument();
  });

  it("shows error alert when activity fetch fails", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.reject(new ApiError("internal", "GitHub API rate limited", 500));
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Error loading activity")).toBeInTheDocument();
    expect(screen.getByText("GitHub API rate limited")).toBeInTheDocument();
  });

  it("passes a stall's and a proposal's fields to the approval card dispatcher", async () => {
    // Each registered card prints what the dispatcher gave it.
    const ShowsPart: ApprovalCardComponent = ({ approval, toolName, projectId }) => (
      <div data-testid={`dispatched-${approval.approval_id}`}>
        {[
          approval.kind,
          String(approval.step),
          String(approval.proposal_kind),
          String(approval.tool_call_id),
          String(toolName),
          projectId,
        ].join("|")}
      </div>
    );
    const registeredStallCard = CARD_FOR_KIND.stall;
    CARD_FOR_KIND.stall = ShowsPart;
    CARD_FOR_PROPOSAL.handoff = ShowsPart;
    const common = {
      agent: "orchestrator",
      preview: { title: "Stalled: no usable key", kind: "text" as const, body: "key 1" },
      status: "pending" as const,
      note: null,
      session_id: "ses_1",
      turn_id: "trn_1",
      created_at: "2026-10-28T12:00:00Z",
      decided_at: null,
    };
    const views: ApprovalView[] = [
      {
        ...common,
        id: "appr_stall",
        kind: "stall",
        tool: null,
        tool_call_id: null,
        risk_class: null,
        step: 3,
        proposal_kind: null,
      },
      {
        ...common,
        id: "appr_handoff",
        kind: "tool",
        tool: "Propose",
        tool_call_id: "call_9",
        risk_class: "propose",
        step: null,
        proposal_kind: "handoff",
      },
    ];
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: views, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    try {
      renderPage("/projects/prj_1/inbox");

      expect(await screen.findByTestId("dispatched-appr_stall")).toHaveTextContent(
        "stall|3|null|null|undefined|prj_1",
      );
      expect(screen.getByTestId("dispatched-appr_handoff")).toHaveTextContent(
        "tool|null|handoff|call_9|Propose|prj_1",
      );
    } finally {
      CARD_FOR_KIND.stall = registeredStallCard;
      delete CARD_FOR_PROPOSAL.handoff;
    }
  });

  it("lists a stall with its key rows, Try again and Stop", async () => {
    const stallView: ApprovalView = {
      id: "appr_stall",
      agent: "orchestrator",
      kind: "stall",
      tool: null,
      tool_call_id: null,
      risk_class: null,
      step: 2,
      proposal_kind: null,
      preview: {
        title: "Stalled: no usable key",
        kind: "text",
        body: "Key 1: daily limit reached (since 12:00 UTC). Key 2: billing, out of credit.",
      },
      status: "pending",
      note: null,
      session_id: "ses_1",
      turn_id: "trn_1",
      created_at: "2026-10-28T12:00:00Z",
      decided_at: null,
    };
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({
          data: [stallView],
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    postSpy = vi
      .spyOn(api, "POST")
      .mockImplementation(() =>
        Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) }),
      );
    const user = userEvent.setup();

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Stalled: no usable key")).toBeInTheDocument();
    expect(screen.getByText("daily limit reached (since 12:00 UTC)")).toBeInTheDocument();
    expect(screen.getByText("billing, out of credit")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(postSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/approvals/{approval_id}/decision",
      expect.objectContaining({
        params: { path: { project_id: "prj_1", approval_id: "appr_stall" } },
        body: { approve: true, note: null },
      }),
    );
  });
});
