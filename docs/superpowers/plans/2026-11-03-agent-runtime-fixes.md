# Agent Runtime and Sandbox Fixes (walkthrough 3) Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. **Every task is its own early PR** (`AGENTS.md` rule 5): work it on `plan/agent-runtime-fixes-task<n>`, created from `origin/main`, and open its PR into `main` when done (`Closes #<task issue>`). There is no plan branch and no plan PR: the plan issue closes when its last task's PR merges. Order: task 4 merges before tasks 2 and 8 (they use its migration), task 7 before task 5; the rest in any order.

**Goal:** fix what stopped the agents in the lead's walkthrough 3 (2026-11-03, findings in the lead's fix list, items 7, 8, 16, 17, 18, 23, 24, 25, 32): an agent could not edit a file twice, its own other task blocked it (so PR #17 went out half-applied), a proposal from a failed turn stayed pending forever, a failed command did not look failed, the sandbox could not reach services on the host or run LightGBM, issue search missed fresh issues, a deploy could run through Bash with no approval, and an agent never learned that its PR's CI was red.

**Branches:** one per task, `plan/agent-runtime-fixes-task<n>`. **Milestone:** Stage 2: Agent runtime. **Consumes:** merged code. **Produces:** `turns.stopped_reason` and the API field in task 4 (the frontend plan shows it).

**Review bar:** the lowered bar (`docs/reviewing-and-merging.md`): real bugs, security and fake features block; minors go to `docs/known-debt.md`.

## Tasks

| Task | Title | Fix |
|---|---|---|
| 1 | An agent can keep editing its own files | 16, 17 |
| 2 | A failed turn leaves no pending proposal behind | 25 |
| 3 | A failed command is a failed tool step | 8 |
| 4 | A turn stopped by the step limit says so | 10 |
| 5 | The sandbox reaches the host and runs common ML libraries | 7, 24 |
| 6 | SearchIssues sees issues opened a moment ago | 18 |
| 7 | A deploy through Bash goes through the deploy gate | 23 |
| 8 | An agent is woken when its PR's CI fails | 32 |

---

### Task 1: An agent can keep editing its own files

**Files:** `agents/tools/edit.py`, `agents/tools/write.py`, `agents/tasks/claims.py`; tests beside their existing tests.

**Notes:**
- After a successful write (`edit.py:154`, `write.py:119`, inside the task pause), record the new content's hash with `tasks.record_read` (the same call `read.py:174` makes), so a second Edit of the same file needs no Read in between. A file changed by anything else since still fails with "changed since you read it".
- The claim conflict query (`claims.py:35-70`) compares task ids only, so the same agent's other open task blocks it. Ignore claims held by tasks of the same agent; another agent's claim still blocks with the same message.

**Tests:** two Edits in a row on one file both succeed (fails on `main`); Write then Edit succeeds; an outside change between them still fails; same agent, two open tasks, same path: the second claim succeeds (fails on `main`); another agent's claim still conflicts.

### Task 2: A failed turn leaves no pending proposal behind

**Files:** `agents/runtime/turn_end.py` (`_close_out`); `proposals/store.py`; tests. (Task 4 also edits `turn_end.py`: whichever merges second brings `main` in.)

**Notes:** a proposal draft is created during the tool's preview, before any approval exists (`tool_step.py:127` → `propose.py:140`). When a turn ends (any status) with drafts it created that never got an approval, mark them `rejected` (`ProposalsStore.update_status`, `expected_status="pending"`), as startup recovery does (`proposals/service.py:327`). Needs the drafts' turn: use the proposal's existing columns if they hold it, else `proposals.turn_id` from task 4's migration (consumes task 4: merge after it).

**Tests:** a turn that fails after a proposal's preview leaves it rejected, not pending (fails on `main`); a proposal waiting on an approval in a finished turn is untouched by this (the approval path handles it).

### Task 3: A failed command is a failed tool step

**Files:** `agents/tools/bash.py`; its tests.

**Notes:** a non-zero exit returns `ToolOutput(msg, is_error=True)` (`bash.py:84-88`), same text. A timeout also returns `is_error=True`. The Failed badge (#731) then shows on it. Before changing it, check every reader of `is_error` (stall and loop guards, repair, stats) for a behaviour change on ordinary non-zero exits such as `grep` with no match; keep the exit code in the text so the model can tell.

**Tests:** exit 1 → `is_error` true with the output; exit 0 → false; timeout → true.

### Task 4: A turn stopped by the step limit says so

**Files:** `storage/tables.py` + project migration `0012` (the plan's only migration: `turns.stopped_reason` text, `tasks.ci_woken_sha` text, `tasks.ci_wakes` integer default 0, and `proposals.turn_id` text if task 2 needs it: check first); merge this task first, tasks 2 and 8 consume its columns; `agents/runtime/graph_run.py`, `executor.py`, `turn_end.py`, `store/turns.py`; `agents/runtime/contract.py` (`TurnEndedData.stopped_reason`, and `SessionView.last_stopped_reason: str | None`, the latest finished turn's); `frontend/openapi.json` regenerated.

**Notes:** `graph.py:193-205` already sets `stopped_reason` (`"max_steps"`, `"room_below_floor"`); carry it out of `run_turn_graph` to the stored turn and the `turn.ended` event and SSE payload. The status stays `done`. **Produces** `SessionView.last_stopped_reason` and `TurnEndedData.stopped_reason` for the frontend plan.

**Tests:** a turn that hits the step limit stores `max_steps` and the session view shows it; a normal turn stores null.

### Task 5: The sandbox reaches the host and runs common ML libraries

**Files:** `sandbox/containers.py` (`build_run_args`, `build_one_shot_run_args`); `sandbox/templates/*.Dockerfile`; `agents/tools/bash.py` (description); tests in `tests/sandbox/`.

**Notes:**
- When network is on, add `--add-host=host.docker.internal:host-gateway` (Linux Docker needs it; Docker Desktop has the name already and accepts the flag). With network off, nothing changes. Measured on Docker Desktop for Windows (2026-11-03): a container with the flag reached a server bound to the host's `127.0.0.1:57250`.
- Bash description, one added sentence: inside the sandbox `127.0.0.1` is the sandbox itself; reach a service on the user's machine at `host.docker.internal` (needs the network grant; on Linux the service must listen on more than `127.0.0.1`).
- **Merge after task 7:** once the sandbox reaches the real tracker, a deploy through Bash changes production; task 7 gates it first.
- Every template installs `libgomp1` (OpenMP runtime that LightGBM, XGBoost and scikit-learn wheels load) with `apt-get install -y --no-install-recommends`, cleaning the apt lists. The image tag changes with the template, so existing images rebuild.

**Tests:** run args with network on hold the `--add-host` flag, with network off don't; each rendered template installs `libgomp1`.

### Task 6: SearchIssues sees issues opened a moment ago

**Files:** `integrations/github/issues.py` (a `list_open_issues(http, token, repo, *, limit)` using `GET /repos/{repo}/issues?state=open`, skipping pull requests); `agents/tools/issues.py` (`SearchIssuesTool`); tests.

**Notes:** the search API (`/search/issues`) lags behind new issues, so in walkthrough 3 two agents opened duplicates minutes apart. SearchIssues lists open issues (up to 300, newest first) and keeps those whose title or body contain every word of the query (case-insensitive); same output format as today.

**Tests:** an issue in the list but not in search results is found; a pull request in the list is skipped; no match → the current "No open issues found" text.

### Task 7: A deploy through Bash goes through the deploy gate

**Files:** `agents/tools/bash.py` (`action_call`); the hooks lookup it needs (`monitoring/hooks`, injected where Bash is built in `api/services.py`); tests.

**Decision (lead, 2026-11-03, recorded in `project_knowledge.md`):** a Bash command that runs one of the project's approved hooks whose `deploys` is true is gated like `RunHook` on that hook. Match: the command contains the hook template's fixed text before its first placeholder (the whole template when it has none), whitespace-normalised. `Bash.action_call` returns that hook's `ActionCall`, so `gate.py` applies `decide_action` with the hook's facts.

**Tests:** at `autonomous`, `uv run <deploy template with a version>` asks (fails on `main`: allowed); a command matching no deploying hook keeps the `run` decision; a non-deploying hook's command keeps it too.

### Task 8: An agent is woken when its PR's CI fails

**Files:** `agents/pr_sync.py`; `integrations/github/merge.py` (move `PASSING_CONCLUSIONS` here from `agents/tools/merge.py`, which imports it); `tasks.ci_woken_sha` and `tasks.ci_wakes` from task 4's migration (consumes task 4: merge after it); `agents/tools/pull_request.py` (description); tests in `tests/agents/`.

**Notes:**
- In `_do_sync`, for each open GitHub task with a PR: read the head sha (`get_pull_merge_state`) and its check runs (`get_check_runs`). Wake only when every run is completed and at least one concluded outside `PASSING_CONCLUSIONS`, and that check's name is not also failing on the default branch's head (a red `main` is not the agent's to fix).
- `_do_sync` runs from the scheduler's `sync_prs` job every collection interval (`monitoring/scheduler/sync_jobs.py`) as well as at turn start, so a quiet project still notices.
- Once per head sha (`ci_woken_sha`), at most 3 times per task (`ci_wakes`). The wake is a system message through `AgentSessions.post(..., author_name="system")` as `agents/reviews.py:116-131` does: the PR number, the task id, the failing check names and conclusions with their details links, and "Resume the task with ResumeTask <id>, reproduce by running the commands of the team's CI workflow in the sandbox, fix it and push with OpenPullRequest (it updates the same PR)." The third wake adds: "This is the last CI wake for this task: if it stays red, say so in your session and stop."
- No job logs (the token may lack Actions read).
- OpenPullRequest description adds: CI runs on the PR; if it fails, you are woken with the failing checks.

**Tests:** a red PR wakes its agent once (fails on `main`); the same sha does not wake again; a new red sha wakes again, up to 3; a check also red on the default branch does not wake; pending runs do not wake; green does not wake.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
