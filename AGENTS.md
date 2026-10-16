# AGENTS.md

Instructions for every coding agent working in this repository (Claude Code, Codex, Cursor, Copilot, Gemini CLI and others) and for the people running them.

These rules apply to every session, whoever runs it and whatever agent they use.

## Status

Design phase: no code yet, so no build, lint or test commands exist. Add them here once the backend and frontend are scaffolded.

## Frontend commands

Run from `frontend/`:
- Install: `npm ci`
- Dev server with hot reload (proxies `/api` to the backend on port 8765): `npm run dev`
- Tests: `npm test` (one file: `npx vitest run src/path/File.test.tsx`)
- Lint and format: `npm run lint`, `npm run format:check`
- Build into the backend package: `npm run build`
- Regenerate API types after the backend API changes: `npm run gen:api`

## Team workflow

Roles:
- Lead: GitHub `yassinebrahem2`. Writes specs and implementation plans, reviews PRs, merges, closes issues. The only person who merges into `main`. Every Decision and Bug issue is assigned to the lead.
- Implementers: everyone, the lead included. Anyone can take plan tasks.

Know which role you are serving: check `gh api user --jq .login`. Everyone follows the implementer rules when working on plan tasks. Only `yassinebrahem2` follows the lead rules (specs, plans, plan issues, reviews, merges, triage).

Lifecycle of a piece of work:
1. The lead writes a spec in `docs/superpowers/specs/` and gets it approved.
2. The lead writes the implementation plans in `docs/superpowers/plans/` (see "How plans are written"). Each plan lives on its own branch `plan/<stage>-<topic>` (for example `plan/stage-1-backend-core`). Every plan task states which tasks it depends on.
3. After a plan is on its branch, the lead opens a plan issue (template "Implementation plan", label `plan`, milestone = its stage). The lead's session then creates one sub-issue per plan task (label `task`, same milestone), each stating the task, its plan section and its dependencies. Everything lands on the project board in "Todo".
4. Implementers pick tasks, work on the plan branch, and move their task through the board.
5. When every task is done, an implementer opens a PR from `plan/<topic>` into `main` (the PR template asks for `Closes #<plan issue>`), requests review from `yassinebrahem2`, and moves the plan issue to "In review".
6. The lead reviews. Merging the PR closes the plan issue and moves it to "Done".

Project board: GitHub Projects (https://github.com/users/yassinebrahem2/projects/5), Status = Todo / In progress / Blocked / In review / Done. Milestones: one per roadmap stage ("Stage 1: Foundation and first agent", ...).
Move a card with `scripts/board-status.sh <issue-or-PR number> "<Status>"` (needs `gh` and `jq`; `gh project` itself needs the `read:org` scope, which most tokens lack).

### Implementer rules
- Work only on the plan's branch. Never push to `main`; never merge anything.
- Claim a task by assigning yourself to its sub-issue before starting. Only take a task that is unassigned and whose dependencies are closed.
- Move your task sub-issue: "In progress" when you start; close it when its work is pushed and its checks pass.
- Pull with rebase before you start and before you push, then re-read the plan (it may have changed). Commit small and push often, since several people share the branch.
- Follow the plan as written. If the plan is wrong, unclear, or needs a decision, do not improvise: open a Decision issue linked to the blocked task, and move that task to "Blocked" until the lead answers.
- Do not edit specs, plans, `docs/roadmap.md` or `project_knowledge.md`; those belong to the lead.
- A task is done only when its tests, lint and type checks pass locally, and CI passes on the branch.

### Lead rules
- Specs before plans, plans before code. Each needs the lead's explicit approval.
- Review specs and plans with a second reviewer (the advisor in Claude Code) until no blocking issues remain.
- Record project-level decisions in `project_knowledge.md` as they are made.
- Answer Decision issues in the issue itself. If the answer changes the plan, update the plan on its branch, then unblock the task.

### How plans are written
- One spec per stage. A stage too big for one plan is split into several plans, each small enough to review and assign on its own.
- A stage overview (`docs/superpowers/plans/<date>-<stage>-overview.md`, on `main`) holds what the plans share: goal, global constraints, review focus, file structure, and the list of plans with their dependencies and branches. Each plan links to it instead of repeating it.
- A plan whose dependencies are all merged gets its branch and plan issue right away. A plan that depends on an unmerged plan waits: its branch is created from `main` and its issue opened only after that plan is merged. Independent plans run in parallel.
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
- Never add AI attribution to commits or PRs: no `Co-Authored-By`, no "Generated with Claude Code".
- PR description follows `.github/pull_request_template.md`: what changed, why, how it was tested, anything not done or done differently, `Closes #<issue>`.
- `main` only changes through PRs merged by the lead (the lead may push spec and roadmap commits directly).

## Code quality
- Follow the conventions in this file, the roadmap contracts and the surrounding code. No exceptions without the lead's approval.
- No silent deferred work. Anything not done, partly done, or done differently from the plan is stated explicitly in the plan issue and the PR description. No `TODO`/`FIXME` left in code as a substitute for saying it.
- No workarounds and no technical debt. Fix the cause, not the symptom. If the right fix is out of reach (blocked, out of scope, needs a decision), stop and raise it in the plan issue instead of patching around it.
- Python: ruff (lint and format) and pyright in strict mode must pass. Type hints everywhere; pydantic models at boundaries (API, config, files). No bare `except`, no swallowed errors.
- TypeScript: `strict` mode, ESLint and Prettier must pass. No `any`.
- Test-first for logic: write the failing test, then the code. Test at the cheapest level that proves the behavior.
- Before saying a task is done, run the tests and checks and report their real output.
- Small focused files; a file past about 400 lines is doing too much. Comments explain why, not what. No commented-out or dead code.
- Match the surrounding code's naming and idioms.

## Documents
- `project_knowledge.md`: what we know about the project (decisions, open questions, ideas, superseded decisions). Facts only, never plans or task notes.
- `docs/roadmap.md`: the build stages and the shared contracts every stage follows (run modes, ID prefixes, data folder layout, SQLite rules, agent definition format, tool risk classes, event/audit records). A change to a contract is made here first.
- `docs/superpowers/specs/`: one design spec per stage. `docs/superpowers/plans/`: implementation plans.

## Project rules
- Never use the Artifact tool. UIs are real, working code in this repo.
- The product UI is dataset-agnostic. Benchmarking on public datasets is separate developer tooling.
- The app name is undecided: `mlops-agents` / `mlops_agents` is a placeholder defined in one place.
- No Anthropic or OpenAI keys are available; development uses Groq, Gemini or Ollama through the providers registry.

## Architecture (from the stage 1 spec)
- `backend/`: Python (uv), FastAPI modular monolith. Domain modules (`storage`, `workspace`, `providers`, `integrations/github`, `agents`) under one `api` layer. One process serves the API, the built UI and the agent runtime; live agent output streams over SSE.
- `frontend/`: React + Vite + TypeScript, Tailwind + shadcn/ui, React Router, TanStack Query. Built into `backend/src/mlops_agents/web/` and shipped inside the wheel.
- Storage: SQLite only. `app.db` plus one `project.db` per project in the app's data folder. Nothing is written into the user's working files.
- Agents: one shared LangGraph runtime; each agent is a markdown definition file with frontmatter. Tools use Claude Code's names and argument shapes; Bash runs only inside a Docker sandbox.
