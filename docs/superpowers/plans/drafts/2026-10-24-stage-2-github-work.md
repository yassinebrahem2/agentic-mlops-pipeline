# Stage 2 — GitHub Work Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Agents publish their work (spec §6):
- pull requests on GitHub, or local reviews for a repository without a GitHub remote;
- issues, with the labels and a minimum priority;
- a reminder at the end of each turn when work is not in a PR;
- tasks that end when their PR is merged or closed.

**Branch:** `plan/stage-2-github-work`. **Consumes:** I8 to I12; events task 1; tasks task 4 (and task 5 for local merge and `end`). **Produces:** the tools OpenPullRequest, SearchIssues, OpenIssue and CommentOnIssue, the PR reminder (`TurnEndCheck`), the PR state sync (a `TurnObserver`), and the routes `GET /reviews`, `POST /reviews/{task_id}/decision`, `GET /github/activity`, and `GET` and `PUT /settings/issues`.

**Read first:** spec §6, all of it.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | GitHub client and the fake GitHub server | runtime-contracts task 1 |
| 2 | OpenPullRequest | task 1, tasks task 4, events task 1, runtime-contracts task 3 |
| 3 | Issue tools | task 1 |
| 4 | Local reviews and GitHub activity | task 2, tasks task 5 |
| 5 | The PR reminder and PR state sync | task 4, runtime-contracts task 4 merged |

---

### Task 1: GitHub client and the fake GitHub server

**Files:** new `backend/src/mlops_agents/integrations/github/pulls.py` and `issues.py` (REST calls, sharing the existing `api.py` request helper), new `backend/tests/fakes/fake_github.py`, tests. `integrations/github/api.py` gains only what the shared helper needs.

**Interfaces:** Consumes stage 1's GitHub auth and HTTP client. Produces:
- `create_pull`, `update_pull`, `get_pull` (state and merged);
- `search_issues` (open issues of one repository);
- `create_issue`, `comment_issue`, `ensure_labels`.

**Implementation notes:**
- The API base URL is one setting the fake overrides in tests.
- A 403 or 404 for missing permission becomes a `DomainError` naming the permission (contents, pull requests or issues: write). The Integrations page's list of needed permissions is one constant that the frontend shows through the existing integration view.
- The fake is a FastAPI app like `fake_openai.py`. It keeps PRs, issues, labels and comments in memory, with routes to inspect them, and lets a test merge or close a PR.

**Tests (stop condition):** each call against the fake; the missing-permission error message; full suite, checks; CI green.

---

### Task 2: OpenPullRequest

**Files:** new `backend/src/mlops_agents/agents/tools/pull_request.py`, `agents/tools/__init__.py` (its line), tests.

**Interfaces:** Consumes task 1, `Tasks.commit`, `push`, `staged_diff` and `set_review`, and `EventLog`. Produces the tool `OpenPullRequest(title, body, fixes=[])` (`github`).

**Implementation notes:** spec §6:
- No current task is the tool error "Start a task first with StartTask."
- The preview shows the title, the body, and `staged_diff`.
- With a `RepoRef`: commit, push, then create or update the PR. The body ends with the agent line and the `Fixes #n` lines. Record `pr.opened` or `pr.updated` with the PR number, URL and title. The task becomes `in_review` (`kind github`).
- With no `RepoRef`: commit, and make the task a local review (`kind local`).
- `CommitRefused` and GitHub errors are the tool's error result.

**Tests (stop condition), against the fake GitHub and a real worktree:**
- First call: a PR with the agent line and `Fixes #3`, and the task `in_review`.
- Second call after a change: same PR, new commit pushed, `pr.updated`.
- A denylisted file: refused, and nothing pushed.
- No `RepoRef`: a local review.
- The preview holds the diff.
- Full suite, checks; CI green.

---

### Task 3: Issue tools

**Files:** new `backend/src/mlops_agents/agents/tools/issues.py`, `agents/tools/__init__.py` (its lines), the issues setting and its routes (`api/routers/github_work.py`), tests, regenerated API types.

**Interfaces:** Consumes task 1. Produces:
- the tools SearchIssues (`read`), OpenIssue and CommentOnIssue (`github`);
- the project setting `issue_min_priority` (default `medium`);
- `GET` and `PUT /settings/issues`.

**Implementation notes:**
- OpenIssue refuses a priority below the minimum, telling the agent to report in its session. Otherwise it ensures the `agent:<name>`, `category:<category>` and `priority:<priority>` labels and opens the issue.
- Events: `issue.opened` and `issue.commented`.
- Without a `RepoRef`, each tool returns the tool error saying the project has no GitHub link.

**Tests (stop condition):** search; open with labels created once; refused below the minimum; comment; no link; the setting round-trips; full suite, checks; CI green.

---

### Task 4: Local reviews and GitHub activity

**Files:** new `backend/src/mlops_agents/agents/reviews.py` (local review listing and deciding), `api/routers/github_work.py` (`GET /reviews`, `POST /reviews/{task_id}/decision`, `GET /github/activity`), tests, regenerated API types.

**Interfaces:** Consumes task 2 and `Tasks.local_merge_preview`, `local_merge` and `end`. Produces the reviews and activity routes.

**Implementation notes:**
- `GET /reviews` lists tasks `in_review` with kind `local`. Each comes with the files of the user's tree that change, and its diff, capped.
- Approve calls `local_merge`, then `end(merged)`. A `CommitRefused` is a 409 with its message, and the review stays.
- Reject calls `end(closed)` and posts a system-labeled message to the agent's session through `AgentSessions.post`, with author `Review` and from_agent the agent itself.
- `GET /github/activity` comes from the `pr.*` and `issue.*` events, plus the PR state of open tasks.

**Tests (stop condition):** approve a clean local review, which merges and ends the task; approve one with a conflict, which gets a 409 and stays; reject one, and the agent gets the message; the activity lists PRs and issues; full suite, checks; CI green.

---

### Task 5: The PR reminder and PR state sync

**Files:** new `backend/src/mlops_agents/agents/pr_reminder.py`, `agents/pr_sync.py`, the services lines (`NoTurnEndCheck` replaced and deleted; the observer added), tests.

**Interfaces:** Consumes task 4 and the hooks. Produces the `TurnEndCheck` and the PR sync `TurnObserver`.

**Implementation notes:**
- The reminder's note is the spec's §6 text, returned when the session's current task `has_unpublished_changes`. The graph already limits it to once per turn.
- `turn_started` starts a background check of the PR state of the project's open `github` tasks, at most once a minute per project, so the turn never waits on GitHub. A merged PR calls `end(merged)`; a closed one calls `end(closed)`. GitHub errors are logged and never fail the turn.

**Tests (stop condition):**
- A turn that changes a file and ends: one reminder step, and none when there is no change.
- A PR merged on the fake: after the next turn start's check finishes, the task is `merged` and its worktree removed. A second turn within the minute makes no new check.
- Full suite, checks; CI green.
