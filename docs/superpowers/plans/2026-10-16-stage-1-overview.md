# Stage 1: Foundation and First Agent — Plans Overview

Stage 1 is built through several plans. This overview holds what they all share. Read it, the spec and `docs/roadmap.md` before starting any plan.

**Goal:** One command installs and starts the app. The user adds AI providers and a project (local folder or GitHub repo), then chats with the Orchestrator, which reads the repo with read-only Read/Glob/Grep tools and streams its answers live. This sits on the shared agent runtime every later agent will use.

**Architecture:** A modular monolith. One FastAPI process (`backend/`) holds domain modules (`storage`, `credentials`, `providers`, `integrations/github`, `workspace`, `agents`) under one `api` layer, and serves a React SPA (`frontend/`) built into the Python wheel. Storage is SQLite only: `app.db` plus one `project.db` per project. Agents run on one LangGraph runtime driven by markdown agent definition files, and answers stream over server-sent events.

**Tech Stack:** Python 3.12, uv, FastAPI, SQLAlchemy Core (async, aiosqlite), LangGraph + langchain-openai, pydantic, keyring, cryptography, sse-starlette, pytest. React 19, Vite 7, TypeScript 5.9, Tailwind 4, shadcn/ui, React Router 7, TanStack Query 5, openapi-typescript + openapi-fetch, Vitest 5, Playwright. GitHub Actions CI.

**Spec:** `docs/superpowers/specs/2026-10-16-stage-1-foundation-design.md`. Shared contracts: `docs/roadmap.md`.

## Plans

| Plan | Branch | Covers | Depends on | State |
|---|---|---|---|---|
| backend-core | `plan/stage-1-backend-core` | Backend scaffold and CI, config, IDs, storage, credentials, git runner, shared test fixtures | none | Merged |
| frontend-scaffold | `plan/stage-1-frontend-scaffold` | React app scaffold, tooling, frontend CI | none | Merged |
| providers | `plan/stage-1-providers` | AI providers registry, fake OpenAI-compatible server, model verification, LangChain bridge | backend-core | Draft in `drafts/`; branch opens when backend-core is merged and the draft converges |
| github-workspace | `plan/stage-1-github-workspace` | GitHub API and auth, local and GitHub projects | backend-core | Ready on its branch |
| agent-tools | `plan/stage-1-agent-tools` | Agent definition files, catalog, tool registry, tracked-file filter, Read/Glob/Grep | backend-core | Draft in `drafts/`; branch opens when backend-core is merged and the draft converges |
| agent-runtime | `plan/stage-1-agent-runtime` | Per-project agent settings and model assignment, message store, context and compaction, model calls with retries, the LangGraph loop, turn executor, stream hub, session queue, Stop, recovery | providers, github-workspace, agent-tools | To write |
| api | `plan/stage-1-api` | App factory, login for both run modes, CLI, every router, SSE stream, committed OpenAPI contract | agent-runtime | To write |
| frontend-app | `plan/stage-1-frontend-app` | API client from the contract, app shell, every stage 1 page | frontend-scaffold, api | To write |
| packaging | `plan/stage-1-packaging` | Build script, wheel contents check, end-to-end test with the fake provider, install check on Windows and Linux | api, frontend-app | To write |

After backend-core is merged, providers, github-workspace and agent-tools run in parallel. frontend-scaffold runs in parallel with all of them.

The five plans written first (two merged, three in `drafts/`) include full implementation code. They were written before the "How plans are written" rule in `AGENTS.md` and are kept as they are. The four plans still to write follow that rule: tests as code or as behaviors to verify, and full code only where a mistake is costly.

## Global Constraints

Every task in every stage 1 plan includes these:

- **Runtimes and backend versions:**
  - Python `>=3.12`; Node 22.
  - Backend dependency floors are the versions verified on 2026-10-16: langgraph 1.2.15, langgraph-checkpoint-sqlite 3.1.2, langchain-openai 1.7.2, langchain-core 1.6.11, fastapi 0.143.2, uvicorn 0.44.0, sqlalchemy 2.1.4, aiosqlite 0.22.1, pydantic 2.13.1, cryptography 46.0.7, python-ulid 4.0.1, platformdirs 4.9.6, sse-starlette 3.3.4, httpx 0.28.1, keyring 25.7.0.
- **Frontend versions:**
  - Majors are pinned to the versions this code is written against: react 19, vite 7, react-router 7, typescript ~5.9, tailwindcss 4, vitest 5, eslint 9, @tanstack/react-query 5, openapi-typescript 7, openapi-fetch 0.17.
  - Newer majors exist (vite 8, react-router 8, typescript 7, eslint 10). Upgrading them is a separate, explicit decision. Vitest went from 3 to 5 on 2026-10-16 by such a decision: vitest 3 depended on packages with critical advisories that no 3.x release fixes.
  - shadcn/ui uses the `radix-vega` style with the neutral base color. shadcn 4 no longer has the `new-york` style.
- **App name:** it appears only in `backend/src/mlops_agents/config.py` (`APP_NAME`), `backend/pyproject.toml` (the project name and the script) and `frontend/src/config.ts` (`APP_NAME`, the UI's single copy). Everything else imports `APP_NAME`.
- **IDs and time:** IDs come only from `mlops_agents.ids.new_id(prefix)`, with prefixes registered in `docs/roadmap.md`. Timestamps come only from `mlops_agents.clock.now_iso()` (UTC ISO 8601).
- **SQLite:** every connection (SQLAlchemy, sqlite3, the LangGraph checkpointer) applies `mlops_agents.storage.sqlite.PRAGMAS`.
- **Secrets:** no key, token or password ever appears in a database row, URL, log line, error message or test snapshot. Errors that may contain one go through `mlops_agents.credentials.scrub`.
- **Blocking work and subprocesses:**
  - File reads, `git`, `gh`, `rg`, keyring and scrypt never run on the event loop; use `asyncio.to_thread`.
  - Subprocesses run via `subprocess.run` inside `asyncio.to_thread`, which works on every Windows event loop. Never use `asyncio.create_subprocess_exec`.
- **Checks:**
  - Python: ruff and pyright (strict) pass. Inline suppressions are allowed only at a third-party boundary, and the suppression names the rule and says why.
  - TypeScript: `strict` mode, ESLint and Prettier pass. No `any`.
- **Error messages:** every error a user can see says what happened and what to do, in plain words.

## Review Focus

These are the inputs most likely to hurt a real user without any single task catching them. The owning plan carries the test for each.

1. **Windows paths.** Backslashes, drive-letter case and mixed case in a local project path must still detect duplicates and resolve tracked files. Tests: github-workspace and agent-tools.
2. **Huge, binary or non-UTF-8 files.** Read and Grep must answer quickly with a clear note, and must never crash or flood the context. Tests: agent-tools.
3. **Unusual provider streams.** Some providers split tool-call arguments across stream chunks, or report no token usage while streaming. Verification and turns must still work. Tests: providers and agent-runtime.
4. **Two tabs, or a reconnect mid-turn.** With two browser tabs on the same session, or a reconnect in the middle of a turn, both tabs show the same answer with no missing or duplicated text. Tests: agent-runtime and api.
5. **A GitHub token that expires or is revoked after a project was added.** Refresh fails with a clear message, and the last good copy stays readable. Tests: github-workspace.

## File structure

```
.github/workflows/      backend.yml, frontend.yml; later plans add contract, e2e and package checks
AGENTS.md               gains Backend commands and Frontend commands sections
scripts/build.py        builds the UI into the backend, then the wheel (packaging plan)
backend/
  pyproject.toml, uv.lock
  src/mlops_agents/
    config.py, paths.py, ids.py, clock.py, errors.py
    storage/            sqlite.py, migrate.py, backup.py, databases.py, tables.py, migrations/
    credentials/        keystore.py, keyring_store.py, file_store.py, scrub.py
    providers/          models.py, service.py, errors.py, bridge.py, verification.py,
                        agent_settings.py, assignment.py
    integrations/github/ git.py, api.py, auth.py
    workspace/          models.py, refs.py, service.py
    agents/
      catalog.py, definitions/orchestrator.md
      tools/            base.py, files.py, read.py, glob.py, grep.py
      runtime/          parts.py, models.py, store.py, events.py, context.py, compaction.py,
                        model_caller.py, graph.py, executor.py, hub.py, sessions.py
    api/                app.py, services.py, security.py, middleware.py, deps.py, errors.py,
                        static.py, openapi_export.py, routers/
    cli.py
    web/                built UI (generated, gitignored)
  tests/                mirrors src; tests/fakes/fake_openai.py is a fake OpenAI-compatible server
frontend/
  openapi.json          committed API contract, generated by the backend
  src/                  see the frontend-app plan
  e2e/                  Playwright test (packaging plan)
```

`credentials/` is a top-level module because both providers and GitHub use it. (The spec groups the keystore under providers.) The spec's behavior is unchanged.
