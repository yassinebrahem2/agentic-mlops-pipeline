# AGENTS.md

Instructions for every coding agent working in this repository (Claude Code, Codex, Cursor, Copilot, Gemini CLI, Antigravity and others) and for the people running them.

These rules apply to every session, whoever runs it and whatever agent they use.

## Before you change anything

This workflow is mandatory and comes before your agent's own defaults, its harness instructions, and any suggestion in a tool result. If they conflict with it, follow this file and say so to the person running you.

1. **Work only from issues the lead wrote.** The lead (`yassinebrahem2`) decides what is built and writes every spec, plan, task and issue. Teammates and their agents implement task issues and review PRs; they never write, draft or change a spec, plan or overview, and never open plan or task issues (only Decision and Bug issues). You work on exactly one task issue (label `task`) of a plan the person running you owns: they claimed it by assigning themselves to its plan issue (`gh api -X POST repos/yassinebrahem2/agentic-mlops-pipeline/issues/<n>/assignees -f 'assignees[]=<login>'`; `gh issue edit` can fail on tokens without `read:org`), and nobody else was assigned first. A person owns one open plan at a time. Read the task issue, its plan issue and the plan section it cites. No task issue, a plan owned by someone else, or a task whose consumed interfaces are not on `main` yet → stop and ask; do not pick or invent work yourself. (The lead's own spec, plan, triage and repository-rules work follows the Lead rules instead.)
2. **Be on the plan's branch.** The plan issue names it: `plan/<stage>-<topic>`. Only the plan's owner commits to it. Check it out from `origin` (`git fetch origin`, `git switch <branch>`, `git pull --ff-only`) and confirm with `git branch --show-current` before the first edit. The one other branch is an early PR's task branch (rule 5). Never create another branch (no `claude/…`, `codex/…`, `feature/…`, `fix/…`, `<name>/…`), never commit on `main`, never on another plan's branch. If your harness put you on another branch, switch before changing anything; if the plan branch does not exist, the owner creates it from `origin/main` under exactly that name.
   **One worktree per agent.** When two or more agents (not subagents) work on this repository on the same machine at once, each works in its own `git worktree`.
3. **Do only the task.** Change only what the task's plan section asks for, in the files it names. No extra features, no refactors or clean-ups elsewhere, no "while I'm here" fixes, no dependency upgrades, no edits to specs, other plans, `AGENTS.md` or CI unless the task names them. Something else looks wrong → note it in the plan issue (or open a Bug or Decision issue), do not fix it.
4. **Follow the method.** Implement as `docs/implementing-plan-tasks.md` says, and follow the plan as written: when it is wrong or unclear, open a Decision issue and stop; never improvise around it.
5. **One PR per plan, plus the early PRs the issues ask for.** By default a finished task is only pushed to the plan branch; no PR. When the plan's last task is done, open one PR from the plan branch into `main` for the whole branch: its description follows `.github/pull_request_template.md` and says `Closes #<plan issue>` (and `Closes #<task issue>` for any task still open). The exception: a task issue whose "PR" section says to open a PR for it (another plan needs it on `main` first) gets its own PR into `main` as soon as it is done (`Closes #<task issue>`), from a branch `plan/<stage>-<topic>-task<n>` created at that task's last commit, so later tasks pushed to the plan branch stay out of it; keep working on the next task while it waits. Request review from `yassinebrahem2`. Bring `main` in with `git merge origin/main`; never rebase or force-push a branch with an open PR. Never merge.
6. **No issue is closed without green CI.** A task with no early PR is closed by its owner once it is done (rule 7): pushed, with CI green on that plan-branch commit. A task with an early PR closes when the lead merges that PR, and the lead merges only when every CI workflow that runs on the PR (Backend, Frontend, and Package and E2E once they exist; every job, including Backend's Windows job) is green on the PR's head and the branch is up to date with `main`. A bug issue follows the same rule through the PR that fixes it; a plan issue closes when its plan PR is merged. Queued, running, red, cancelled or missing CI means no merge, whatever the local checks say. If CI is red for a reason outside your task, raise it in the plan issue. (A Decision issue carries no code; it closes when the lead has answered it.)
7. **Keep the board true.** Every time your issue changes state, move its card to the matching column yourself, then check it moved; automation can be late or fail, so never assume it did:

   | When | Card |
   |---|---|
   | you start a task (assign yourself) | In progress; its plan issue too, on the plan's first task |
   | you stop on a Decision issue, a failing check you cannot fix, or anything else outside your control | Blocked (and say why in the issue) |
   | the blocker is resolved and you resume | In progress |
   | you stop working on it and unassign yourself | Todo |
   | the task is done, pushed and CI is green (no PR of its own) | close the issue: Done |
   | a PR into `main` is opened | every issue it closes: In review (In review holds only work waiting on the lead) |
   | the lead requests changes on your PR | the PR and every issue it closes: In progress (until you push the fixes) |
   | you push the fixes for a PR with changes requested | the PR and every issue it closes: In review |
   | the issue is closed (rule 6) | Done |

   Move a card with `scripts/board-status.sh <number> "<Status>"` (needs `gh`, `jq` and a token with the `project` scope) or, where the Projects API is not reachable (cloud agent sessions), by running the `board` workflow with `workflow_dispatch` inputs `number` and `status` (cloud sessions block `gh workflow run`, which uses GraphQL; use the REST call `gh api -X POST repos/yassinebrahem2/agentic-mlops-pipeline/actions/workflows/board.yml/dispatches -f ref=main -f 'inputs[number]=42' -f 'inputs[status]=Blocked'`, or your agent's GitHub tool for a workflow dispatch, e.g. the GitHub MCP `actions_run_trigger`). Check that the run succeeded. If you cannot move the card, say so in the issue so a person moves it in the GitHub UI. Column names are exactly: Todo, In progress, Blocked, In review, Done.

## Backend commands

Run from `backend/`:
- Install: `uv sync`
- Tests: `uv run pytest` (one test: `uv run pytest tests/path/test_file.py::test_name -v`)
- Live tests against real services: `uv run pytest -m live`
- Lint and format: `uv run ruff check` and `uv run ruff format`
- Types: `uv run pyright`

## Frontend commands

Run from `frontend/`:
- Install: `npm ci`
- Dev server with hot reload (proxies `/api` to the backend on port 8765): `npm run dev`
- Tests: `npm test` (one file: `npx vitest run src/path/File.test.tsx`)
- Lint and format: `npm run lint`, `npm run format:check`
- Build into the backend package: `npm run build`
- Regenerate API types after the backend API changes: `npm run gen:api`

## Team workflow

The team is five people, each working with their own coding agent. The work is split so that each person can work on their own, at the same time as the others: every plan has one owner, plans are small, plans depend on interfaces rather than on whole plans, and every task merges into `main` on its own.

Roles:
- Lead: GitHub `yassinebrahem2`. Decides what is built. Writes the specs, the stage overviews, every plan, every task and every issue; owns the interfaces between plans; does the final review of every PR, merges, closes plan issues. The only person who merges into `main`. Every Decision and Bug issue is assigned to the lead.
- Plan owners: everyone, the lead included. Teammates pick plans themselves: whoever first assigns themselves to an unassigned plan issue whose Consumes are on `main` owns it. One open plan per person at a time; the lead does not hand out plans. Exception: when your plan's next task waits on another plan's task that is not on `main` yet, you may claim a second plan meanwhile. The owner implements that plan's task issues on the plan's branch and opens the plan's PR (and the early PRs its issues ask for). Owners do not write or change plans, tasks or issues (Decision and Bug issues aside). Nobody else commits to that branch. An owner who must hand a plan over says so in the plan issue; the lead names the new owner.
- Reviewers: the lead reviews every PR. A teammate review before the lead's is welcome but optional; teammates pick reviews themselves: anyone other than the PR's author claims the review by commenting "Reviewing" on the PR (first comment wins), then submits it with `gh pr review <n> --comment` / `--request-changes` / `--approve`; the owner of a plan that consumes what the PR produces goes first when they can. The reviewer checks the PR against its task and the interfaces they depend on, and comments on the PR; the reviewer never pushes to the branch.

Know which role you are serving: check `gh api user --jq .login`. Everyone follows the owner rules on their own plan. The lead rules belong to the lead's Claude Code session only, whose `CLAUDE.md` says so; the login is not enough, because the lead's other agents (Antigravity, Codex, ...) use the same account. Any other agent on `yassinebrahem2` is an owner on the lead's plan and never does a lead action: no merging, closing, approving or requesting changes on a PR; no push to `main`; no change to specs, plans, the overview, `AGENTS.md` or `project_knowledge.md`; no plan or task issues; no answering or closing Decision issues; no closing plan issues. (GitHub cannot tell these agents apart; this rule is the only guard.)

Lifecycle of a piece of work:
1. The lead writes a spec in `docs/superpowers/specs/` and gets it approved.
2. The lead splits the stage into plans in the stage overview (`docs/superpowers/plans/<date>-<stage>-overview.md`). For each plan the overview gives its scope, its branch, and two lists with exact names and types: **Consumes** (interfaces already on `main` or produced by another plan) and **Produces** (interfaces other plans build on). These cross-plan interfaces belong to the lead; changing one takes a Decision issue.
3. The lead writes each plan (see "How plans are written"), as a draft in `docs/superpowers/plans/drafts/` until a review round with the advisor finds no blocking issues, then moves it out of `drafts/` on `main`.
4. The lead opens the plan issue (template "Implementation plan", label `plan`, milestone = its stage, left unassigned for an owner to claim) and one sub-issue per task (label `task`, same milestone), each stating the task, its plan section and the interfaces it consumes. New issues land on the board in "Todo" automatically.
5. The owner creates the branch `plan/<stage>-<topic>` from `origin/main` and works through the tasks in order, pushing each finished task to the branch. A task whose issue asks for an early PR gets one into `main` right away; the owner moves on while it is reviewed. When the last task is done, the owner opens the plan's PR for the whole branch.
6. The lead reviews and merges each PR (rule 6), after an optional teammate review. Merging closes the issues it names: an early PR its task, the plan PR the plan issue.

Project board: GitHub Projects (https://github.com/users/yassinebrahem2/projects/5), Status = Todo / In progress / Blocked / In review / Done. Milestones: one per roadmap stage ("Stage 1: Foundation and first agent", ...).
Move a card with `scripts/board-status.sh <issue-or-PR number> "<Status>"` (needs `gh` and `jq`, and a token with the `project` scope; `gh project` itself also needs `read:org`, which is why the script uses the GraphQL API).
Rule 7 above makes every agent and person keep their own cards right; the automation is a backup, not a replacement. Most moves are also automated: `.github/workflows/board.yml` adds every new issue to Todo, the board's built-in workflows set Done on close and merge, and `board.yml` moves a PR and the issues it closes to In progress when changes are requested and back to In review on the owner's next push, sets In progress when a `task` issue is assigned (Todo when its last assignee is removed), Blocked when a `decision` issue names the task under "Blocked task" (In progress again when that Decision closes), and In review when a PR into `main` says `Closes #<n>`. Cloud agent sessions cannot reach the board API; they move a card by running the `board` workflow manually (`workflow_dispatch` with `number` and `status`, see rule 7).

### Owner rules
- Work only on your plan's branch. Never push to `main` or to another plan's branch; never merge anything.
- Do the tasks in the plan's order. Assign yourself to a task's sub-issue when you start it and move the cards as rule 7 says.
- Before each task and before each PR, bring `main` in (`git fetch origin`, `git merge origin/main`) and re-read your plan and the overview's interface lists (they may have changed). Commit small and push often.
- Generated files are regenerated after a merge, never merged by hand: `backend/uv.lock` (`uv lock`), `frontend/package-lock.json` (`npm install`), `frontend/openapi.json` (re-export it from the backend as the frontend commands say).
- Implement each task the subagent-driven way described in `docs/implementing-plan-tasks.md`: a fresh implementer per task, a fresh reviewer checks it against the plan task and the spec, fixes are re-reviewed before the task is pushed.
- Every PR leaves `main` working: tests, checks and the app pass after it merges on its own (an early PR included). The UI only shows navigation for pages that work, so a page's menu entry merges with or after the page.
- Follow the plan as written. If the plan is wrong, unclear, or needs a decision, do not improvise: open a Decision issue linked to the blocked task, move that task's card to "Blocked" yourself (rule 7), and move it back to "In progress" when the lead's answer lets you resume.
- Do not edit specs, plans, stage overviews, `docs/roadmap.md` or `project_knowledge.md`, and do not open plan or task issues; those belong to the lead.
- A task is done only when its tests, lint and type checks pass locally and the CI run on the plan branch commit holding it is green; then you close its issue, unless it has an early PR (that PR's merge closes it).
- Reviewing a teammate's PR is optional; early PRs are the ones worth picking up promptly, especially those producing interfaces your plan consumes: a waiting early PR holds another owner's next task. An agent asked to review a PR reads its task issue, the plan section and the interfaces the reviewer's own plan consumes, runs the tests, and posts its findings as a PR review (Critical / Important / Minor, as in `docs/implementing-plan-tasks.md`). It never pushes to the PR's branch.

### Lead rules
- Specs before plans, plans before code. Each needs the lead's explicit approval.
- Review specs, the stage overview's interface lists and every plan draft with a second reviewer (the advisor in Claude Code) until no blocking issues remain.
- Review every PR yourself (after the teammate's review when there is one), against its tasks, the spec and the interfaces. Merge only with green CI on the PR's head and the branch up to date with `main`, always with a merge commit (never squash or rebase: the plan branch shares an early PR's commits). Right before merging, the lead may bring `main` into the PR with GitHub's Update branch; that merge commit is the one exception to "only the owner commits".
- Review each PR with its own review subagent, all in parallel, so no review waits for another.
- Record project-level decisions in `project_knowledge.md` as they are made.
- Answer Decision issues in the issue itself. If the answer changes a plan or an interface, update the plan or the overview on `main` first, then unblock the task.

### How plans are written
- One spec per stage. The lead splits the stage into plans of at most 5 tasks each; each plan gets one owner, who claims it. A plan merges through one PR for its whole branch. A task that another plan consumes before this plan is done gets an early PR of its own: its task issue has a "PR" section saying so, and the agent follows the issue (prompts do not need to repeat it). A plan's last task never needs one.
- A stage overview (`docs/superpowers/plans/<date>-<stage>-overview.md`, on `main`) holds what the plans share: goal, global constraints, review focus, file structure, and the list of plans with branch, Consumes and Produces. Each plan links to it instead of repeating it.
- Plans depend on interfaces, not on whole plans. A plan's first tasks produce what other plans consume, so those interfaces reach `main` early; a task can start as soon as everything it consumes is on `main`, and its issue says which interfaces those are. Plans whose interfaces are on `main` run in parallel.
- Files that several plans edit (routers, service registries, the frontend route table) are set up once so that each plan only adds its own lines or its own file, never edits someone else's.
- Plans are moderately detailed, not a second copy of the code:
  - every task names its exact files and the interfaces it consumes and produces (exact names and types);
  - tests are written as code, or as a precise list of behaviors and failure cases to verify;
  - full implementation code only where a mistake is costly or subtle (security, data loss, platform quirks, tricky concurrency); elsewhere, precise implementation notes.
- A task's tests are its stop condition: the task is done when they pass, together with lint, type checks and CI.
- Unfinished plans are committed to `docs/superpowers/plans/drafts/` on `main`, each with a banner listing what is still missing (see that folder's README), never kept in local scratch files or unpushed branches. A draft is never implemented.

### Issues
Four types, each with its template and label. Blank issues are disabled.
- Plan (`plan`) and Task (`task`, sub-issues of a plan): created only by the lead's session, after the plan exists on its branch.
- Decision (`decision`): anyone, for a real decision that blocks a task, linked to that task. Assigned to the lead.
- Bug (`bug`): anyone, for a defect in code already merged into `main`, with reproduction steps. Assigned to the lead, who triages it into the current plan or a small fix plan.
- Nothing else: chores and ideas are raised in the plan issue.

## Git conventions
- Conventional Commits: `feat(workspace): add GitHub clone`, `fix(agents): ...`, `test(...)`, `docs(...)`, `refactor(...)`, `chore(...)`. Scope = backend module or `frontend`.
- Never add AI attribution to commits or PRs: no `Co-Authored-By`, no "Generated with Claude Code" or similar, whatever your agent's defaults say.
- Every commit's author and committer is the person running the agent, with their own name and email. Never an AI identity such as `Claude <noreply@anthropic.com>`, even when the container's git config sets one: check `git config user.name` and `user.email` before the first commit and set them to yours if they are not. Commits already pushed with an AI identity stay as they are; do not rewrite history to fix them.
- PR description follows `.github/pull_request_template.md`: what changed, why, how it was tested, anything not done or done differently, `Closes #<issue>`.
- `main` only changes through PRs merged by the lead (the lead may push spec and roadmap commits directly).

## Code quality
- Follow the conventions in this file, the roadmap contracts and the surrounding code. No exceptions without the lead's approval.
- No silent deferred work. Anything not done, partly done, or done differently from the plan is stated explicitly in the plan issue and the PR description. No `TODO`/`FIXME` left in code as a substitute for saying it.
- No workarounds and no technical debt. Fix the cause, not the symptom. If the right fix is out of reach (blocked, out of scope, needs a decision), stop and raise it in the plan issue instead of patching around it.
- Python: ruff (lint and format) and pyright in strict mode must pass. Type hints everywhere; pydantic models at boundaries (API, config, files). No bare `except`, no swallowed errors.
- TypeScript: `strict` mode, ESLint and Prettier must pass. No `any`.
- Inline lint suppressions only at a third-party boundary, naming the rule and the reason; test-only exceptions go in the linter's per-file config.
- Test-first for logic: write the failing test, then the code. Test at the cheapest level that proves the behavior.
- Before saying a task is done, run the tests and checks and report their real output.
- Small focused files; a file past about 400 lines is doing too much. Comments explain why, not what. No commented-out or dead code.
- Match the surrounding code's naming and idioms.

## Documents
- `project_knowledge.md`: what we know about the project (decisions, open questions, ideas, superseded decisions). Facts only, never plans or task notes.
- `docs/roadmap.md`: the build stages and the shared contracts every stage follows (run modes, ID prefixes, data folder layout, SQLite rules, agent definition format, tool risk classes, event/audit records). A change to a contract is made here first.
- `docs/implementing-plan-tasks.md`: how every agent implements a plan task (implementer, reviewer, fix loop).
- `docs/superpowers/specs/`: one design spec per stage. `docs/superpowers/plans/`: implementation plans.

## Project rules
- Never use the Artifact tool. UIs are real, working code in this repo.
- The product UI is dataset-agnostic. Benchmarking on public datasets is separate developer tooling.
- The app name is undecided: `mlops-agents` / `mlops_agents` is a placeholder defined in one place.
- No Anthropic or OpenAI keys are available; development uses Groq, Gemini or Ollama through the providers registry.
- Secrets (provider keys, GitHub tokens, `MLOPS_AGENTS_PASSWORD`, `MLOPS_AGENTS_MASTER_KEY`) never go in a config file, a URL, a log line, an error message, a `repr`, a test fixture or a commit.
- Runtime data (`app.db`, `project.db` files, worktrees, `keys.enc`) lives in the app's data folder outside the repository (`MLOPS_AGENTS_HOME` overrides it). Never run `git clean -x`/`-X` or any recursive delete whose target could reach the data folder; scope every cleanup to a named path inside the repo.
- Scaffolding CLIs (create-vite, shadcn, uv init, etc.) must be run with their non-interactive flags; never leave a dev server running at the end of a task.

## Architecture (from the stage 1 spec)
- `backend/`: Python (uv), FastAPI modular monolith. Domain modules (`storage`, `workspace`, `providers`, `integrations/github`, `agents`) under one `api` layer. One process serves the API, the built UI and the agent runtime; live agent output streams over SSE.
- `frontend/`: React + Vite + TypeScript, Tailwind + shadcn/ui, React Router, TanStack Query. Built into `backend/src/mlops_agents/web/` and shipped inside the wheel.
- Storage: SQLite only. `app.db` plus one `project.db` per project in the app's data folder. Nothing is written into the user's working files.
- Agents: one shared LangGraph runtime; each agent is a markdown definition file with frontmatter. Tools use Claude Code's names and argument shapes; Bash runs only inside a Docker sandbox.
