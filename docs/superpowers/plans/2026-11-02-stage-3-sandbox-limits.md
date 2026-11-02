# Stage 3: Every Agent Knows What the Sandbox Can Do Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #705 and the agent-behaviour findings of the lead's walkthrough (2026-11-02). Onboarding and all four audit agents spent their steps on what the sandbox cannot do: `uv run pytest` (no network, missing system libraries), `apt-get install`, Python's `import duckdb` (duckdb is a CLI); Model Performance hit its step limit. Onboarding ran Bash before any connection was approved, announced "now let's propose" and ended its turn without proposing, explored three of five folders and asked the user for what the other two held, and did not report its failed steps. duckdb's output carried terminal colour codes into the model's and the user's view (#705).

**Branch:** `plan/stage-3-sandbox-limits`. **Milestone:** Stage 3: Onboarding and data access. **Consumes:** merged code. **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue, its task issue and #705.

**Read first:** bug #705; `backend/src/mlops_agents/agents/runtime/context.py` (`working_rules`, the section every agent's system message carries); `sandbox/containers.py` (the container's environment and limits), `sandbox/templates/`; `agents/definitions/onboarding.md` and `onboarding/briefs.py`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | One statement of the sandbox's limits; Onboarding's working rules | merged code |

---

### Task 1: One statement of the sandbox's limits; Onboarding's working rules

**Files:** `backend/src/mlops_agents/agents/runtime/context.py` (`working_rules`), `sandbox/containers.py` (the environment); `agents/definitions/onboarding.md`; tests under `backend/tests/agents/runtime/` and `backend/tests/sandbox/`.

**Notes:**
- `working_rules` adds, for an agent that has Bash, the sandbox's limits, written once and built from the sandbox's own settings where they exist (network off unless granted, the memory and CPU limits): no network, no system package installs (changes do not persist), the project's own test suite may not run there and that is reported as a gap rather than retried, duckdb is the `duckdb` command, an environment failure is reported once and not retried in variants. Not seven copies in the definitions.
- The container's environment sets `DUCKDB_AGENT_MODE=1` (clean, compact output; verified by the walkthrough companion).
- `onboarding.md`: no Bash before a `connections` proposal is approved; act instead of announcing the next step; look in every folder found before asking; report every step that failed in the final answer. Prompt rules only: they help, they do not guarantee a weak model follows them.
- Exit 137 in the walkthrough is not explained; do not state its cause.

**Tests (stop condition):** first, one that fails on `main`: an agent with Bash gets the limits section, one without Bash does not, and it names the network setting the sandbox actually has; the container command carries `DUCKDB_AGENT_MODE=1`; the onboarding definition still loads; backend suite, ruff, pyright; CI green.

## Known debt added

- The read-only phase before connection approval is a prompt rule, not enforced in code.
