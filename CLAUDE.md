# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Every team member works with Claude Code, so these rules apply to every session, whoever runs it.

## Status

Design phase: no code yet, so no build, lint or test commands exist. Add them here once the backend and frontend are scaffolded.

## Team workflow

Roles:
- Lead: GitHub `yassinebrahem2`. Writes specs and implementation plans, reviews PRs, merges, closes issues. The only person who merges into `main`.
- Implementers: the other team members. Carry out implementation plans.

Know which role you are serving: check `gh api user --jq .login`. If it is `yassinebrahem2`, follow the lead flow; otherwise follow the implementer flow.

Lifecycle of a piece of work:
1. The lead writes a spec in `docs/superpowers/specs/` and gets it approved.
2. The lead writes the implementation plan in `docs/superpowers/plans/` on a new branch `plan/<topic>` (for example `plan/stage-1-foundation`).
3. After the plan is on its branch, the lead opens a plan issue (template "Implementation plan", label `plan`): it links the plan and the branch and lists the plan's tasks as checkboxes. It goes on the project board in "Todo".
4. Implementers work on that branch, following the plan, and move the issue to "In progress".
5. When the plan is done, an implementer opens a PR from `plan/<topic>` into `main` (the PR template asks for `Closes #<issue>`), requests review from `yassinebrahem2`, and moves the issue to "In review".
6. The lead reviews. Merging the PR closes the issue and moves it to "Done".

Project board: GitHub Projects, Status = Todo / In progress / In review / Done.

### Implementer rules
- Work only on the plan's branch. Never push to `main`; never merge anything.
- Before starting a task, claim it in the issue (comment, or check off as you finish) so two people do not do the same task.
- Pull with rebase before you start and before you push; commit small and push often, since several people share the branch.
- Follow the plan as written. If the plan is wrong, unclear, or needs a decision, do not improvise: comment on the issue and ask the lead.
- Do not edit specs, plans, `docs/roadmap.md` or `project_knowledge.md`; those belong to the lead.
- A task is done only when its tests, lint and type checks pass locally, and CI passes on the branch.

### Lead rules
- Specs before plans, plans before code. Each needs the lead's explicit approval.
- Review specs and plans with the advisor until no blocking issues remain.
- Record decisions in `project_knowledge.md` as they are made.

### Issues
- The lead opens only plan issues, after the plan exists on its branch.
- Implementers may open an issue only for a real decision that needs the lead or the team (template "Decision", label `decision`). Not for bugs, chores or ideas: those are handled inside the plan's work or raised in the plan issue.

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
