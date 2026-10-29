import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import {
  CARD_FOR_KIND,
  CARD_FOR_PROPOSAL,
  type ApprovalCardComponent,
} from "@/pages/agents/ApprovalCard";
import type { ApprovalView } from "./usePendingApprovals";
import { projectStub, sampleApprovals, sampleReviews, TestFakeStream } from "./testing";

describe("InboxPage and badge", () => {
  let fakeStream: TestFakeStream;
  let getSpy: MockInstance | undefined;
  let postSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

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
        title: "groq: no usable key for llama-3.3-70b",
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

    expect(await screen.findByText("groq: no usable key for llama-3.3-70b")).toBeInTheDocument();
    expect(screen.getByText("Stalled")).toBeInTheDocument();
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
