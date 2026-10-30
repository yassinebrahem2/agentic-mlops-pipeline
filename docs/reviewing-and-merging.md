# Reviewing and merging PRs (lead)

How the lead's Claude Code session reviews and merges PRs, locally or in a cloud session. It applies AGENTS.md (rules 5 to 7, Lead rules, Code quality); it adds no new rules. Only the lead's Claude Code session does this (AGENTS.md, "Know which role you are serving").

## 1. Find what needs the lead

- Locally, run `scripts/lead-watch.sh` under the Monitor tool (re-arm it when it expires): it prints each new PR head, each head whose CI finished, and each card that needs the lead. `scripts/board-list.sh [open|closed|all]` lists the board.
- Open PRs: `gh pr list --json number,author,headRefName,reviewDecision,isDraft,headRefOid,updatedAt`.
- A PR needs the lead when it is not a draft and either has no review yet or has new commits since the last "changes requested".
- Board: only the open cards in In review and Blocked matter; a closed issue anywhere but Done is a wrong card (step 6).

## 2. One review subagent per PR

Start one background subagent per PR, all at once, on model `sonnet`, each told to call the advisor before it reports and to post nothing to GitHub. Its prompt names:
- the plan and task (`docs/superpowers/plans/...`, the task issue numbers) and the interfaces it produces or consumes (`docs/superpowers/plans/2026-10-16-stage-1-overview.md`, I1 to I7);
- the diff to review: `git diff origin/main...origin/<head branch>` (only the new task's commits if earlier tasks are already on main);
- for a re-review: "verify each point of the lead's last review and review only the commits since that head; no new hunt for Minors";
- for a demo-company PR: the company's planted choices (listed in the demo-world plan) are deliberate and never findings;
- the merge gate (below), so its verdict follows it; a removed-fix check (revert the fix, the test must fail) only for Important fixes;
- the checks: the plan's behaviour and its tests (a test that cannot fail is a finding), real bugs on normal and real failure paths, no secrets anywhere, the hard limits, no data loss, no stub or fake feature, the interfaces the plan produces and consumes; DRY, separation of concerns, file size and style only when they cause a real problem now; PR body (template, `Closes #<plan issue>`, honest "Not done" and "Known debt added", no AI attribution, no control characters);
- to run backend (`uv sync`, `uv run pytest`, `ruff check`, `ruff format --check`, `pyright`, `python -m mlops_agents.api.openapi_export --check`) and/or frontend (`npm ci`, `npm test`, `npm run lint`, `npm run format:check`, `npm run build`) in a new `git worktree` under the scratchpad, never switching the main checkout, and to remove that worktree afterwards;
- to return Critical / Important / Minor with `file:line`, a verdict (merge / changes) and the head SHA.

Verify a Critical finding in the code yourself before acting on it. Reviewers can be wrong about process: under rule 6 an owner closes a task without an early PR once it is done, so "closed by hand" is not a finding for such tasks.

## 3. Post the verdict

- Changes: `gh pr review <n> --request-changes --body-file <file>`, short: the Important items, then Minor. The board moves the PR and its open issues to In progress.
- Your own PR (`yassinebrahem2` is the author): GitHub refuses approve and request-changes, so `gh pr comment <n> --body-file <file>` and move the cards yourself (`scripts/board-status.sh <n> "In progress"` for the PR and its open issues).
- Write review bodies to a file and pass `--body-file`; inline backslashes become control characters.
- **Merge gate** (2026-10-29). Only Critical and Important findings hold a merge: red CI, a broken spec or interface contract, a missing or fake feature, touching the user's repo, refs or config, a secret leak, a broken hard limit, data loss, a crash at startup or shutdown, a 500 or wrong answer on a normal path, a test that passes on broken code. Aim for one round. Everything else is Minor (a rare path that fails loudly, a large file, duplication, style, an edge case that cannot occur, PR description accuracy): approve and merge, and in the same step add each Minor that leaves a real shortcut or gap (a copy, a missing check, a test-only function, an oversized file) to `docs/known-debt.md` (one line: where, what, why accepted, the fix); a Minor that is a real behaviour bug goes in a Bug issue instead; pure naming and style nits are not debt and are not tracked. Say which in the approval. Nothing is dropped silently.
- End every review and comment to an owner with: work autonomously, check your PR and Blocked cards about every 10 minutes (rule 7), keep going on your next task meanwhile.
- A bug filed into a plan or a task's "Review fixes" is reassigned from `yassinebrahem2` to that plan's owner in the same step.
- Before answering a Decision issue or changing a plan or an interface, get a second review (the advisor in Claude Code), and check the plan's other tasks for clashes with the answer.
- Commit messages never say "fixes #n" or "closes #n" unless the commit really closes that issue: GitHub closes it on push to `main`.

## 4. Merge

All of these, every time (rule 6, Lead rules):
1. The verdict is merge.
2. CI on the PR's head: every job completed and green (`gh pr checks <n>`), including Backend's three Windows shards (`backend (windows-latest 1/3)` to `3/3`) and Package's `install` jobs. Queued, running, cancelled or red means wait.
3. Up to date with `main`: `gh api repos/yassinebrahem2/agentic-mlops-pipeline/compare/main...<head branch> --jq .behind_by` is 0. If not, update the branch (`gh api -X PUT repos/yassinebrahem2/agentic-mlops-pipeline/pulls/<n>/update-branch -f expected_head_sha=<head>`), wait for CI on the new head, then check again.
4. Approve (`gh pr review <n> --approve --body "..."`; a comment on your own PR), then merge with a merge commit pinned to the reviewed head:
   `gh api -X PUT repos/yassinebrahem2/agentic-mlops-pipeline/pulls/<n>/merge -f merge_method=merge -f sha=<head>`. Never squash or rebase.
5. Delete the head branch (`git push origin --delete <branch>`) unless an owner's worktree still uses it, and check that the issues it closes are closed.
6. Tell the owners whose work this unblocks, in their plan issue.

Fix small PR-body problems yourself instead of another round: a stale test count, a real `#token=` value (replace with `<redacted>`), a stray AI footer. Edit with `gh api -X PATCH repos/yassinebrahem2/agentic-mlops-pipeline/pulls/<n> -F body=@<file>`.

## 5. Board

- Columns: Todo, In progress, Blocked, In review, Done. Move a card with `scripts/board-status.sh <n> "<Status>"`.
- In a cloud session the Projects API is not reachable: run the `board` workflow instead, `gh api -X POST repos/yassinebrahem2/agentic-mlops-pipeline/actions/workflows/board.yml/dispatches -f ref=main -f 'inputs[number]=<n>' -f 'inputs[status]=<Status>'`, and check the run succeeded.
- `board.yml` moves cards on PR open, on "changes requested" and on the owner's next push; it skips closed issues. Owners' agents sometimes move a closed issue back to In review by hand: put it back in Done.
- A Blocked card is right only while what it waits for is not on `main`; when its blocker merges, move it to In progress (or Todo if nobody has started) and tell the owner.

## 6. Known traps

- Windows: `git worktree remove` fails on long `node_modules`/`.venv` paths. Prune the registration (`git worktree prune`) and leave the folder; it is outside the repository.
- A plan branch that carries several early PRs' tasks: an early PR is opened from `plan/<stage>-<topic>-task<n>` (rule 5). If a later PR's branch also contains an earlier unmerged task, merge the earlier PR first and update the later branch.
- Generated files after a merge: `frontend/openapi.json` must match a fresh export (`openapi_export --check` in CI); owners regenerate it rather than resolving conflicts by hand.
- Frontend tests are CPU-bound in testing-library's role queries (#120): run `npm test` with no other test suite or review agent running at the same time, or a page test can time out.
- Python on the lead's Windows machine: use `py -3.12` for helper scripts; bash heredocs with backslashes are fragile, so write scripts and bodies to files.
