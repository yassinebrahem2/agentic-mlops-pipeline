# Stage 2 — Tooling Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Two tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, interfaces, tests (its stop condition) and checks.

**Goal:** Fix bugs #283 (uv is not pinned, so `uv.lock` churns between machines) and #284 (agent definition tests: the live test was not run and asserts too little, the spec-table test reads only some fields, `STAGE_2_TOOLS` is declared twice).

**Branch:** `plan/stage-2-tooling-fixes`. **Consumes:** merged code. **Produces:** the pinned uv version, which every later plan that regenerates `uv.lock` uses. It merges before stage 3 upgrades DuckDB.

**Read first:** bugs #283 and #284; `AGENTS.md` (generated files are regenerated, never merged by hand).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Pin uv (#283) | merged code |
| 2 | Agent definition tests (#284) | merged code |

---

### Task 1: Pin uv (#283)

**Files:** `backend/pyproject.toml`, `backend/uv.lock`, every workflow in `.github/workflows/` that installs uv.

**Implementation notes:**
- A floor, not an exact pin, for people: `[tool.uv] required-version = ">=<first version that writes the current lock format>"` in `pyproject.toml`, so a teammate whose uv came from pip or winget is not locked out.
- An exact version only in CI: the current stable release (at or above the floor), defined once per workflow file, never per job.
- Regenerate `uv.lock` with the CI version. The diff changes the lock's header fields only, no package versions.

**Tests (stop condition):** `uv lock --check` passes in CI on Linux and Windows; full backend suite green.

### Task 2: Agent definition tests (#284)

**Files:** `backend/tests/agents/test_agent_definitions.py` (declares its own `STAGE_2_TOOLS` at line 42), `backend/tests/conftest.py` (declares `STAGE_2_TOOLS` at line 15).

**Implementation notes:**
- `STAGE_2_TOOLS` is declared once, in `tests/conftest.py`; `test_agent_definitions.py` imports it.
- The spec-table test checks every column of the stage 2 spec's agent table (`2026-10-24-stage-2-agent-runtime-design.md`, the table headed `| Agent | Chat | Tools | May message | Wake-ups |`) for every agent: chat, tools, may message (`can_message`) and wake-ups.
- The live test (marked `live`) asserts that each chat agent answers a short prompt with text and that a tool-using agent calls one of its tools. Run it once against a real provider and paste its output in the PR.

**Tests (stop condition):** the tests above; removing a tool from one definition file fails the spec-table test; full backend suite, ruff, format, pyright; CI green on every job.
