# Stage 2 — Tasks Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Agents change code on their own branch, in their own worktree, safely (spec §2, §3, and the committing and local-merge parts of §6):
- the one function that runs git on a task;
- worktrees without secret files;
- StartTask and ResumeTask;
- Write and Edit;
- file claims;
- committing;
- the local merge;
- discarding and cleaning up.

**Branch:** `plan/stage-2-tasks`. **Consumes:** I8 to I12, events task 1; task 5 also needs sandbox task 3. **Produces:** the `Tasks` implementation (I12), the tools StartTask, ResumeTask, Write and Edit, and `POST /tasks/{task_id}/discard`.

**Read first:** spec §2, §3 and §6 (Fetching and pushing, Committing, the local review steps), all of them. This plan holds the security rules of stage 2: read the "Host git on a worktree" paragraph twice.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The task git function, worktrees and the current task | runtime-contracts tasks 1 and 2, events task 1 |
| 2 | StartTask, ResumeTask and reading in a task | task 1, runtime-contracts task 3 |
| 3 | Write and Edit, with claims | task 2 |
| 4 | Committing and pushing | task 3 |
| 5 | Local merge, ending, discarding, and wiring | task 4, sandbox task 3, runtime-contracts task 4 merged |

Task 4 gets an early PR, from `plan/stage-2-tasks-task4`: github-work's OpenPullRequest needs it.

---

### Task 1: The task git function, worktrees and the current task

**Files:** new `backend/src/mlops_agents/agents/tasks/git.py`, `agents/tasks/worktrees.py`, `agents/tasks/service.py`, tests. Not wired yet: task 2 wires it.

**Interfaces:** Consumes I8, I12, the `tasks` table, `github_remote`/`https_url`, stage 1's `GitRunner` and `RepoFiles`, `EventLog`. Produces `Tasks.start`, `resume`, `current`, `files`, `view` and `open_tasks`.

**Implementation notes:**
- `tasks/git.py` is the only code that runs git on a task. Every call passes:
  - `GIT_DIR` and `GIT_WORK_TREE` from the task record (never trusting the worktree's `.git` entry);
  - `-c core.hooksPath=<data folder>/empty-hooks`, `-c core.fsmonitor=false`, `-c core.longpaths=true`;
  - `GIT_SSH_COMMAND="ssh -o BatchMode=yes"`.

  Fetch and push take the `https_url` of the project's `RepoRef`, with the token through stage 1's runner.
- Creating a worktree:
  1. For a project with a `RepoRef`, fetch the default branch into `refs/<app name>/base/<default branch>` (a GitHub project's managed clone, or the local repository).
  2. `git worktree add -b agent/<agent>/<slug>-<6 chars of the task id> <projects/<id>/worktrees/<tsk>> <base>`.
  3. `git update-index --skip-worktree` on every tracked file that `files.is_secret` matches, then delete those files.
- The slug comes from the title: lowercase ASCII, hyphens, at most 40 characters.
- A local project's `origin` URL is read with `git remote get-url origin` and passed to `github_remote`.
- `files(task)` is a `RepoFiles` variant over `git ls-files --cached --others --exclude-standard`, minus the denylist, never outside the root. Its listing cache is dropped by `invalidate()`, which Write, Edit and Bash call after they change anything.
- `start` records `task.started`.

**Tests (stop condition), on real temporary repositories:**
- A local repository with a tracked `.env` and `config.pem`: the worktree lacks both, `git status` there is clean, and the user's `.git/config` is byte-for-byte unchanged.
- With a fake remote: the test sets `GIT_CONFIG_GLOBAL` to a temporary config with `url."<file URL of a bare repository>".insteadOf "https://github.com/<owner>/<repo>.git"`. Production code has no test-only path. The base ref is fetched into the namespaced ref, and `origin/*` is untouched.
- The worktree's `.git` file rewritten to point at a planted repository whose config sets `core.fsmonitor` to a script: a task git call still uses the task's repository, and the script never runs.
- `files` sees a new untracked file, never a denylisted or ignored one.
- Full backend suite, ruff, format, pyright; CI green on Linux and Windows (long paths included).

---

### Task 2: StartTask, ResumeTask and reading in a task

**Files:** new `backend/src/mlops_agents/agents/tools/tasks.py` (both tools), `agents/tools/read.py` (record the read hash), `agents/tools/__init__.py` (its lines), tests. Tasks 2 to 4 are tested with a test `ToolContext` that carries a task. Their tools are registered, but no definition lists them until agent-definitions, and the session uses the repository until task 5 wires the real `Tasks`.

**Interfaces:** Consumes task 1. Produces the tools StartTask and ResumeTask (`write_code`), and `Tasks.record_read` and `read_hash`.

**Implementation notes:**
- StartTask is refused while the current task `has_unpublished_changes`. Until task 4 lands, a task with any change in its worktree counts as unpublished. ResumeTask refuses an unknown task or another agent's task. Both answer with the task's title, branch and worktree.
- Read, when the context has a task, records the sha256 of the file's bytes.

**Tests (stop condition):** start, then resume another of the same agent's tasks; refusals for another agent's task and an unknown id; Read records a hash only inside a task; full suite, checks; CI green.

---

### Task 3: Write and Edit, with claims

**Files:** new `backend/src/mlops_agents/agents/tools/write.py`, `agents/tools/edit.py`, `agents/tasks/claims.py`, `agents/tools/__init__.py` (their lines), tests.

**Interfaces:** Consumes tasks 1 and 2. Produces the tools Write and Edit (`write_code`, with diff previews), and `Tasks.claim`.

**Implementation notes:** exactly spec §3:
- Claude Code's arguments.
- No current task is the tool error "Start a task first with StartTask."
- Path resolution inside the worktree, refusing denylisted names, opening without following symlinks, and checking the resolved path again at open time.
- The read-hash check, done again when the call runs after an approval.
- Edit's uniqueness rules.
- Bytes kept, with the file's line endings.
- `preview` returns the unified diff.
- The output is the diff, capped at 30,000 characters.
- A successful call claims its path, then calls `files.invalidate()`. A path claimed by another open task fails with the `ClaimConflict` message.

**Tests (stop condition):**
- New file, overwrite after Read, and overwrite without Read refused.
- A file changed after Read refused, and that check repeated at run time.
- Edit with a missing `old_string`, with a duplicate one, and with `replace_all`.
- A CRLF file keeps CRLF.
- A symlink planted to escape the worktree is refused, and so is a denylisted name.
- The claim conflict names the other agent.
- The preview equals the applied diff.
- Full suite, checks; CI green on Linux and Windows.

---

### Task 4: Committing and pushing

**Files:** new `backend/src/mlops_agents/agents/tasks/commit.py`, tests.

**Interfaces:** Consumes tasks 1 and 3. Produces `Tasks.has_unpublished_changes`, `staged_diff`, `commit` and `push`.

**Implementation notes:** spec §6, Committing, through the task git function:
- Stage everything.
- Refuse, after unstaging, when a staged path is denylisted or claimed by another open task.
- Otherwise claim every staged path.
- Commit as the user: their name and email from git config, else their GitHub login and the noreply address.
- `staged_diff` stages, diffs against the base, and unstages, capped at 60,000 characters.
- `push` pushes the branch to the https URL.
- (Added in the review of #241.) After a push, the pushed sha is recorded in the namespaced ref `refs/<app name>/pushed/<branch>`, which `has_unpublished_changes` compares against; no `refs/remotes/*` ref is ever written or deleted.
- `has_unpublished_changes` is true when the worktree has changes, or the branch has commits its remote branch lacks. With no `RepoRef`, it is true when the branch is ahead of its base and no local review exists.

**Tests (stop condition):**
- A Bash-like change (a file written directly) is committed.
- A new `.env` blocks the commit, naming it, and nothing stays staged.
- A path claimed by another task blocks it.
- The author is never an AI identity.
- A push to a fake remote lands the branch.
- `has_unpublished_changes` in each case.
- Full suite, checks; CI green.

---

### Task 5: Local merge, ending, discarding, and wiring

**Files:** new `backend/src/mlops_agents/agents/tasks/local_merge.py`, `agents/tasks/lifecycle.py`, `backend/src/mlops_agents/api/routers/tasks.py`, the session manager's `current_task` in `SessionView` (the one line that reads it), stage 1's project removal (`git worktree prune`, deleting `agent/*` branches in a local repository), tests.

**Interfaces:** Consumes task 4, `Sandbox.remove_task` (sandbox task 3), and runtime-contracts task 4's hook points. Produces `Tasks.local_merge_preview`, `local_merge`, `set_review` and `end`, `POST /tasks/{task_id}/discard`, `SessionView.current_task`, and the wiring: the real `Tasks` replaces `RepositoryOnlyWorkspaces` in `services.py`, which is deleted.

**Implementation notes:**
- `local_merge`, exactly spec §6:
  1. Build the merge commit in a temporary worktree in the data folder. A conflict raises `CommitRefused` naming the files, and nothing in the user's repository changes.
  2. In the user's repository, run `git merge --ff-only <commit>`, with no hooks and no fsmonitor. When the default branch is checked out in none of the repository's worktrees, update the branch ref with `git update-ref <ref> <new> <old>` instead.
  3. Remove the temporary worktree.
- `local_merge_preview` lists the files of the user's working tree that step 2 would change.
- `end(status)`:
  - removes the task's container (`Sandbox.remove_task`), then its worktree (`git worktree remove --force`, then stage 1's read-only-aware delete);
  - deletes the branch when merged or discarded;
  - removes its claims;
  - records `task.ended`.
- Discard is refused with 409 while the agent's turn is running or waiting.
- At turn start, a task whose worktree is missing gets it recreated from its branch. A note that uncommitted changes were lost is added to the next tool result of that task; no message is posted, since that would queue a turn. This goes in `Tasks.current`.

**Tests (stop condition):**
- Local merge with the default branch checked out and clean: the files change and the branch fast-forwards.
- With an uncommitted change in the way: refused, nothing changes.
- With the branch not checked out: only the ref moves.
- A conflicting change: refused, nothing changes.
- A `post-merge` hook in the user's repository does not run.
- `end` removes the worktree, branch and claims. Discard during a running turn returns 409.
- Removing a project prunes its worktrees and branches.
- Wired: a session turn that calls StartTask then Write (in two steps) writes into the task's worktree, and Read in a later turn sees it.
- Full suite, checks; CI green on Linux and Windows.
