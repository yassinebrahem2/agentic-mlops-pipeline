# Stage 2 — Fixes Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Fix the small defects found in stage 2 reviews: #216 and #217 (chat layout) and #226 (the GitHub https URL built twice).

**Branch:** `plan/stage-2-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** bug issues #216, #217 and #226; `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Composer Stop with whitespace, and chat layout leftovers (#216, #217) | merged code |
| 2 | One GitHub https URL builder (#226) | merged code |

---

### Task 1: Composer Stop with whitespace, and chat layout leftovers (#216, #217)

**Files:** `frontend/src/pages/agents/Composer.tsx`, `frontend/src/app/shell/AppShell.tsx`, and their tests, plus the layout test in `frontend/src/pages/agents/AgentsPage.test.tsx`. If frontend-agents (#205) has merged changes to that test file, bring in `main` first and change only the layout test.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- `showStop` uses the same notion of "empty" as `canSend`: text that is empty after trimming. Derive both from one `trimmed` value.
- Remove `sticky top-0` from the sidebar wrapper in `AppShell.tsx`. The shell root no longer scrolls, so the class has no effect.
- The agents layout test asserts that the element before the message log is the header row, by its role or accessible name, not merely that a sibling exists.

**Tests (stop condition):**
- While busy, with only spaces or a newline typed, the button is Stop, and clicking it calls stop. Typing a letter turns it into Send.
- The layout test fails if the header row is removed.
- `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 2: One GitHub https URL builder (#226)

**Files:** `backend/src/mlops_agents/workspace/refs.py`, `backend/src/mlops_agents/workspace/service.py`, `backend/tests/api/test_github_routes.py`, and the refs and workspace tests.

**Interfaces:** Consumes merged code (`RepoRef`, `https_url`, `github_remote`). Produces none; `https_url` stays the one builder.

**Implementation notes:**
- Delete `github_https_url` from `service.py`. The clone's default `remote_url` builds the URL with `refs.https_url` from a `RepoRef` made from the `owner/repo` full name.
- `RepoRef` is built from a full name in one function in `refs.py`, which `github_remote` uses too.
- Tests that imported `github_https_url` use `refs.https_url`.
- Afterwards, `git grep -n "https://github.com/" -- backend/src` finds the URL form in `refs.py` only.

**Tests (stop condition):** cloning a GitHub project uses the same URL as before (the existing clone tests pass); the grep above; full backend suite, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`; CI green.
