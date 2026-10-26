# Stage 2 — Cleanup Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. One PR for the branch, no early PRs (`AGENTS.md` rule 5). Each task lists its files, interfaces, tests (its stop condition) and checks.

**Goal:** Fix the Minor findings filed at merge in bugs #265 (github-work), #261 (tasks), #264 (sandbox) and #258 (approvals and runtime), bug #273 (the status board reports a turn running after `turn.ended`) and bug #276 (recording `waiting` has no fallback), so stage 2 ships with no known defect.

**Branch:** `plan/stage-2-cleanup`. **Consumes:** merged code. **Produces:** `Tasks.repo_ref`, `Tasks.has_commits_beyond_base` and `Tasks.decide_review` (task 1; used only inside this plan's files).

**Read first:** the four bug issues; the stage 2 overview (I8–I12, the Tasks interface); `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | One owner for the repo reference and the branch's state (#265) | merged code |
| 2 | Ending, discarding and local merge (#261) | task 1 |
| 3 | One wording for every failed build (#264) | merged code |
| 4 | Approvals and runtime tidy-up (#258, #273, #276) | runtime-fixes (#250) and messaging (#266) merged |

Task 4 edits `agents/runtime/sessions.py`, `worker.py` and `executor.py`, which PRs #250 and #266 also change: start it only after both are merged. Tasks 1 to 3 don't touch those files.

The PR-description items of the bugs (#264 item 2, #265 item 8) were corrected by the lead on the merged PRs #263 and #262; no code.

---

### Task 1: One owner for the repo reference and the branch's state (#265)

**Files:** `backend/src/mlops_agents/agents/tasks/service.py`, `agents/tasks/push.py`, `agents/tools/pull_request.py`, `agents/tools/issues.py`, `agents/pr_sync.py`, `agents/reviews.py`, `integrations/github/` (the 404 mapping), the activity route, their tests.

**Interfaces:** Produces on `Tasks` (added to the overview's Tasks interface in the same commit as this plan):
```python
async def repo_ref(self, project_id: str) -> RepoRef | None: ...
async def has_commits_beyond_base(self, project_id: str, task: TaskRef) -> bool: ...
async def decide_review(self, project_id: str, task_id: str,
                        step: Callable[[TaskRef], Awaitable[None]],
                        status: Literal["merged", "closed"]) -> TaskView: ...
# Decision #275: under one acquisition of the task lock, raises a 409 domain error unless the
# task is in local review, runs step (local_merge for approve, the system message for reject),
# then ends the task with status (an inner, lock-held end).
```

**Implementation notes:**
- `Tasks.repo_ref` replaces the four copies (`_resolve_repo_ref` in `pull_request.py`, `issues.py` and `pr_sync.py`, and `project_repo_ref` in `push.py`); nothing reads `workspace._git` or `_paths` through `getattr`.
- `Tasks.has_commits_beyond_base` replaces `pull_request.py`'s `_has_commits_beyond_base`, through the task git function from the task's stored `base_sha` (Decision #275: one source, works for local-only projects too); no `except Exception`. `has_unpublished_changes` uses the same rev-list helper, so "commits beyond a ref" has one owner in `push.py`.
- `decide_local_review` goes through `Tasks.decide_review` (Decision #275); the module-level `_review_locks` goes.
- Reject (spec §6: "Rejecting closes the task, and a system message tells the agent"): post the system message first, then end the task `closed`. A failed post leaves the task in review and the route answers the error (never a 500); a second decision on a task no longer in review answers 409, so a retry never closes or posts twice.
- `get_pull` reports a 404 as "not found" (the PR was deleted), not as a missing permission.
- Activity items for issues show the state last recorded in the event log (`issue.commented` records it from `get_issue`). An issue closed on GitHub by a merged PR (`Fixes #n`) still shows its last recorded state; the PR says so (Decision #275).
- The rejection reaches the agent as a system message (`author_name="system"`), not as a message from the task's own agent.
- The `PytestUnhandledThreadExceptionWarning` seen once in a review run did not reproduce (Decision #275): no code change; the `-W error` run below stays as the check.

**Tests (stop condition):** one test per item above (the reject case with a failing post, the 404, the issue state, the sender); `git grep -n "_resolve_repo_ref\|project_repo_ref\|_review_locks" backend/src` finds nothing; `uv run pytest -W error::pytest.PytestUnhandledThreadExceptionWarning` passes; full suite, ruff, format, pyright; CI green.

---

### Task 2: Ending, discarding and local merge (#261)

**Files:** `agents/tasks/service.py` (`end`, `current`), `agents/tasks/local_merge.py`, `api/routers/tasks.py` (discard), `tests/fakes/workspaces.py` and the tests that import it.

**Implementation notes:**
- `end` refuses a task that has already ended with a domain error (no event), which the discard route maps to 409; local merge and reject, which call `end` on a task in review, keep working.
- The discard route's project scan treats one unreadable project as "not there" and logs it; it never answers 500 for that.
- `local_merge` uses `workspace/fs.rmtree` (its own `_rmtree` goes), runs under the per-task lock, and uses a fresh temp path per merge.
- No `suppress(Exception)` in `end` or the merge cleanup: failures are logged with their traceback.
- Reading the session view (`SessionView.current_task`) never recreates a missing worktree; the turn path keeps its current behaviour.
- The name `RepositoryOnlyWorkspaces` is gone from the tests (alias and imports).

**Tests (stop condition):** a second discard of a merged task answers 409 with one `task.ended` in total; one broken project doesn't turn discard into a 500; a cancel mid-merge leaves no temp worktree and the user's repo untouched; reading a session view with a deleted worktree doesn't recreate it; `git grep -n RepositoryOnlyWorkspaces` finds nothing; full suite, checks; CI green.

---

### Task 3: One wording for every failed build (#264)

**Files:** `backend/src/mlops_agents/sandbox/images.py`, `sandbox/service.py`, `tests/sandbox/test_service_env.py`.

**Implementation notes:** the runner `OSError` path in `build_image` goes through `_build_failure_detail` like every other failure path.

**Tests (stop condition):** a runner `build` raising `OSError("boom")` gives `detail == "Image build failed: boom"`; full suite, checks; CI green.

---

### Task 4: Approvals and runtime tidy-up (#258, #273, #276)

**Files:** `agents/approvals/` (`store.py`, `service.py`, `waiting.py`), `agents/runtime/sessions.py`, `worker.py`, `executor.py` (split, behaviour unchanged), their tests.

**Implementation notes:**
- `decide` keeps the approval's stored `task_id`; it never takes the task current at decision time.
- `waiting.py` uses the typed observer contract, not `hasattr(sink, "turn_waiting")`.
- `ApprovalsService` takes one events dependency, not `audit_log` and `event_log` separately.
- `sessions.py` reads the session view through its typed contract, not `getattr`/`inspect.isawaitable` (#261 item 5).
- #276: recording `waiting` in `worker._take_turn` goes through the same shielded fallback as every other end (Decision #249): logged, `turn.waiting` published, observers notified, never raises.
- #273: `turn_states` agrees with `turn.ended`. A session whose turn has ended counts as running only if another turn has started; a worker that is only finishing its loop doesn't count. Fix the state, not the test.
- `sessions.py`, `worker.py` and `executor.py` each go under 400 lines by moving one concern each into its own module; behaviour unchanged.

**Tests (stop condition):**
- The concurrent-decision test fails when the `status='pending'` guard is removed, and the restart test fails when `ensure_recovered` is removed from `pending` or `decide` (check both by reverting and restoring).
- A decision with another task current keeps the approval's `task_id`.
- A storage error while recording `waiting`, with a second turn queued: the second turn runs.
- `turn_states` called right after `turn.ended` (with the worker held just before it exits) reports no running turn; `test_session_manager_turn_states_running_worker` passes 50 times in a loop.
- `wc -l` on the three runtime files is under 400 each; the runtime tests pass 10 times in a loop.
- Full suite, checks; CI green.
