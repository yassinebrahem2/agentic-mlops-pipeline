# Stage 2 — Cleanup Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, interfaces, tests (its stop condition) and checks.

**Goal:** Fix bug #288, found in the review of #286.

**Branch:** `plan/stage-2-cleanup-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use; every public interface keeps its signature.

**Read first:** bug #288; the cleanup plan (`2026-10-26-stage-2-cleanup.md`) and Decisions #275, #278 and #281; `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Cleanup review fixes (#288) | merged code |

---

### Task 1: Cleanup review fixes (#288)

**Files:** `backend/src/mlops_agents/agents/runtime/worker.py`, `executor.py`; `backend/src/mlops_agents/integrations/github/issues.py` (`get_issue`); `backend/src/mlops_agents/agents/pr_sync.py`; `backend/src/mlops_agents/agents/tasks/service.py`, `push.py`, `local_merge.py`; their tests.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- **Item 1, one true turn state.** Invariant: once `turn.waiting` is recorded, `turn_states` reports the session as `waiting`, never `running`. Order the worker so the session leaves the running set (`_turn_over`) before `turn.waiting` reaches any observer.
- **Item 2, a failed `waiting` write.** If writing `waiting` fails, the turn does not stay half-waiting: it ends as failed with the error logged, and it tries at once to expire its pending approvals through the same `record_decisions` path recovery expiry uses. If that also fails (the store itself is failing), that is logged too, and the existing startup recovery expires them.
- **Item 3, a deleted issue.** GitHub's "Get an issue" answers 410 Gone for a deleted issue, and 404 when the issue does not exist *or the token cannot see it* (cite GitHub's REST docs for the endpoint in the PR). So a 404 keeps meaning "permission missing" (`request()` in `integrations/github/api.py`), and only a 410 means the issue is gone: `get_issue` raises its own `issue_gone` error for it, and the PR sync skips that issue with a debug-level log (off by default) instead of a warning on every sync. The fake GitHub server answers 410 for a deleted issue.
- **Item 4, one failing issue does not stop the rest.** Already true: `request()` turns the HTTP client's transport errors (`httpx2.HTTPError`) into `DomainError`, and the per-issue refresh in `pr_sync.py` catches `DomainError` and continues. No code change; add the regression test below.
- **Item 5, file size.** `agents/tasks/service.py` is 410 lines. Move the discard scan into its own module; `Tasks` keeps its signatures. The review steps stay where they are, since `decide_review` holds the task lock once (Decision #275).
- **Item 6, one rmtree.** `push.py`'s `_rmtree` with `suppress(Exception)` goes; it uses `workspace/fs.rmtree` (which already clears the read-only flag git sets on Windows), and a failed cleanup is logged, never swallowed.
- **Item 7, no leaked temp folder.** A cancel during `mkdtemp` in `local_merge.py` (line 139) leaves no folder: once it exists, it is removed on every way out.

**Tests (stop condition):**
- An observer that calls `turn_states` inside its `turn_waiting` callback sees `waiting`.
- A store that raises when writing `waiting`: the turn ends as failed, its approvals are expired through `record_decisions`, and the error is logged.
- `get_issue` on a 410 raises `issue_gone`, and on a 404 still raises the permission error. The sync skips a gone issue without a warning and refreshes the next one.
- A transport error on one issue: the next issue is still refreshed.
- A failed cleanup in `push.py` is logged, and removing a read-only file in a temp push folder works on Windows (runs in the Windows CI job).
- A cancel right after `mkdtemp` leaves no folder.
- Full backend suite, ruff, format, pyright; CI green on every job.
