# Implementing plan tasks

How every coding agent implements a plan task in this repository, whatever agent it is. It fits the team workflow in `AGENTS.md` (one owner per plan, work on the plan branch, one PR per plan plus the early PRs task issues ask for, Decision issues for anything the plan does not settle), which applies in full.

The method: you (the controller) never write the task's code yourself. A fresh implementer does the task, a fresh reviewer checks the result against the plan task and the spec, fixes are made and re-reviewed, and only then does the next task start. Fresh context for each role keeps the reviewer from inheriting the implementer's assumptions.

## Roles

- **Controller**: the session the person started. Claims the task, prepares the brief, dispatches the implementer and the reviewer, records progress, closes the task. Does not write the task's code.
- **Implementer**: one fresh subagent per task. Writes the code and tests, runs the checks, commits, reports. Never spawns subagents of its own.
- **Reviewer**: one fresh subagent per review. Reads the diff and the code, not the implementer's report. Changes nothing.

If your agent cannot run subagents, play each role yourself in turn, with a clean break between them: finish and commit the implementation, then start the review from the brief and the diff only (a new session or a cleared context where your agent allows it), as if someone else wrote the code.

Never run two implementers at once on the same branch, and never let an implementer dispatch another agent.

## Workspace

Keep working files in `.agent-work/` at the repository root (git-ignored). Per task:
- `.agent-work/task-<issue>-brief.md`: the brief (below);
- `.agent-work/task-<issue>-report.md`: the implementer's report;
- `.agent-work/task-<issue>-review.md`: the review package and the reviewer's verdict;
- `.agent-work/ledger.md`: one line per finished task, for example `Task 3 (#14): complete (commits a1b2c3d..e4f5a6b, review clean)`, plus the Minor findings left open.

The ledger lets a new session pick up where an interrupted one stopped. State other people need (claims, blockers, what is left) goes in the task's issue, not only in the ledger.

## Per-task loop

### 1. Set up
1. Do the checks in `AGENTS.md`, "Before you change anything": one task of a plan you own, whose consumed interfaces are on `main`, on the plan's branch (`git branch --show-current` shows `plan/<stage>-<topic>`, never a branch you created), scope limited to the task. Move the task's card to In progress (AGENTS.md rule 7). Bring `main` in (`git fetch origin`, `git merge origin/main`) and re-read the plan and the overview's interface lists. Put the branch name and the task's scope in the brief, and have the implementer and the reviewer check both: a commit on another branch or a change outside the task is a Critical finding.
2. Pre-flight: read the plan task, the plan's Global Constraints and Review Focus, the stage overview and the spec sections the task cites. If the task is wrong, unclear or contradicts the spec or the code on the branch, stop here: open a Decision issue (see the last section) and move the task's card to Blocked; move it back to In progress when you resume.
3. Record the base commit: `git rev-parse HEAD`. Every diff for this task is taken from this base, never from `HEAD~1` (the task may have several commits, and merges from `main` land on the branch).
4. Write the brief: the full task text copied from the plan (not a link), the plan's Global Constraints and Review Focus items that touch it, the spec excerpts it needs, the interfaces it consumes from earlier tasks (exact names and types, read from the code on the branch), the commands to run (from `AGENTS.md`), and the base commit.

### 2. Implement
Dispatch a fresh implementer with the brief and these instructions:
- Work test-first: write the failing test, run it and keep the failing output (RED), write the code, run it again and keep the passing output (GREEN).
- Do exactly what the task says. No extra features, no refactors outside the task's files, no changes to specs or plans.
- Run every check the task's area requires (tests, lint, format, type checks) and fix what they report.
- Self-review before reporting: every behaviour the task lists is implemented and tested; the tests check behaviour, not mocks; names match the plan's interfaces exactly; no `TODO`, dead code, `any`, bare `except`, swallowed error or secret; files stay small and focused.
- Commit with the message the plan gives (Conventional Commits, no AI attribution).
- Report to the report file, ending with one status:
  - `DONE`: everything done and checked;
  - `DONE_WITH_CONCERNS`: done, with doubts listed (an assumption, a weak test, a plan detail that looked wrong);
  - `BLOCKED`: cannot finish; what blocks it and what was tried;
  - `NEEDS_CONTEXT`: a question the brief does not answer.
  The report also lists the files changed, the commits, the RED and GREEN test output and the final check output (real output, not a summary).

On `NEEDS_CONTEXT`, answer from the plan, spec or code and resume the same implementer; if the answer is not in them, it is a decision (see the last section). On `BLOCKED`, find the cause: missing context (give it), a task too big for the model (use a more capable one), or a plan problem (Decision issue).

### 3. Review
Build the review package from the base you recorded:
```
git log --oneline <base>..HEAD
git diff --stat <base>..HEAD
git diff -U10 <base>..HEAD
```
Dispatch a fresh reviewer with the brief, the review package and these instructions:
- Do not trust the implementer's report. Read the code and run the tests and checks yourself.
- Spec compliance: for each requirement in the task, ✅ met (with the file and line), ❌ missing or wrong, ⚠️ unclear. Also flag anything built that the task did not ask for.
- Code quality: correctness, edge cases and failure paths (the plan's Review Focus first), tests that would catch a regression, the rules in `AGENTS.md`, and consistency with the surrounding code.
- Rate each finding: **Critical** (wrong behaviour, data loss, security, a broken check), **Important** (a missing requirement or test, a rule broken, a likely bug), **Minor** (style, naming, small clarity). Rate by the effect on a real user or on the next task, not by how much there is to say.
- End with a verdict: clean, or the list of findings to fix.

### 4. Fix loop
If the review has Critical or Important findings:
1. Send the findings to the implementer: rounds 1 to 3 resume the same implementer; rounds 4 and 5 use a fresh implementer on a more capable model, given the brief, the findings and the current diff.
2. After each round, a fresh reviewer re-reviews only the findings and the code the fix touched (new package from the same base), and confirms nothing else regressed.
3. At most 5 rounds. If findings remain after that, stop: the task is not done. Say so in the task's issue and raise it with the lead.

Minor findings do not hold the task. Fix them in the loop if cheap; otherwise list them in the ledger and in the plan issue (see the last section).

### 5. Finish
1. Check the final state yourself: tests, lint and type checks pass on the branch head.
2. Bring `main` in again (`git merge origin/main`; regenerate `uv.lock`, `package-lock.json` or `openapi.json` instead of merging them by hand), run the checks again if anything came in, push.
3. Wait for the CI runs on the pushed commit (every workflow and job, Backend's Windows job included); a red run is a finding for the fix loop.
4. If the task issue has a "PR" section asking for an early PR, open it now into `main` from a new branch `plan/<stage>-<topic>-task<n>` at the task's last commit (review fixes go on that branch, then merge it into the plan branch) (template `.github/pull_request_template.md`, `Closes #<task issue>`) and move the card to In review. Otherwise close the task issue once CI on the pushed commit is green (the card goes to Done). Answer review comments through the same fix loop. An early PR's task closes when that PR is merged (AGENTS.md rule 6). Add the ledger line, then start the next task, and keep checking the early PR while you work (AGENTS.md rule 7).

## Choosing models

Always name the model when you dispatch a subagent. Use the cheapest tier that can do the job: mechanical work (a small, fully specified change) on the cheapest; integration work (several files, existing interfaces) on the standard tier; design-heavy or subtle work (concurrency, security, data loss, platform quirks) and every final review on the most capable.

## The plan PR

When the last task is done, run one review of the whole branch on the most capable model: package from `git merge-base origin/main HEAD` to HEAD, checked against the plan's Goal and Review Focus and the spec sections the plan covers, looking at how the tasks fit together rather than at each task again. Fix Critical and Important findings through the same loop, then open the plan PR: `Closes #<plan issue>` (and `Closes #<task issue>` for any task still open).

## Rules that hold throughout
- **No rulings by the agent.** When the plan is wrong, unclear or needs a decision, do not choose for the lead: open a Decision issue linked to the task, naming it under "Blocked task" (the board moves the task to Blocked and back when the Decision closes). Continue only after the lead answers.
- **No silent deferral.** Open Minor findings, concerns from a `DONE_WITH_CONCERNS` report, and anything done differently from the plan are written in the plan issue and, at the end, in the PR description. The ledger alone is not enough.
- **One task at a time.** Work only on your own plan's tasks, in order; only you commit to its branch. Bring `main` in with `git merge origin/main`, never rebase or force-push a branch with an open PR.
- **Handoffs in the issue.** If you stop before a task is done, comment on its issue: what is committed, what is left, the base commit. The next person (or session) starts from that comment.
- **Checks are real.** A task is done only when its tests, lint and type checks pass locally and CI on its pushed commit is green; report the real output. No issue is ever closed without green CI.
