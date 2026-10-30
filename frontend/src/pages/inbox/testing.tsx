import { vi } from "vitest";
import { STREAM_CLOSED, STREAM_OPEN, type StreamLike } from "@/pages/agents/session/stream";
import type { ApprovalView, GitHubItem, LocalReview } from "./usePendingApprovals";

export class TestFakeStream implements StreamLike {
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

export const sampleApprovals: ApprovalView[] = [
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

export const sampleReviews: LocalReview[] = [
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

export const sampleActivity: GitHubItem[] = [
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

export const projectStub = {
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
