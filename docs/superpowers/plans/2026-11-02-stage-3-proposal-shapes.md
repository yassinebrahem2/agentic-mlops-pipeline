# Stage 3: An Agent Can Read a Proposal's Shape Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #704. `Propose` declares `payload: Any`, so a model never sees the fields of a `connections` or `handoff` proposal; in the lead's walkthrough Onboarding failed three `connections` and nine `handoff` attempts and the user had to step in. Some rules are also invisible: an unknown on-call is `operations.on_call: []`, and command placeholders accept only eight names.

**Branch:** `plan/stage-3-proposal-shapes`. **Milestone:** Stage 3: Onboarding and data access. **Consumes:** merged code. **Produces:** the read tool `ProposalShape` (name to confirm against the tools registry's naming) used by every agent that has `Propose`. The plan PR says `Closes` the plan issue, its task issue and #704.

**Read first:** bug #704; `backend/src/mlops_agents/agents/tools/propose.py`, the tools registry and a read-class tool for the pattern; `proposals/payloads.py` (every kind's payload model, `PLACEHOLDERS` or the placeholder constant, `OnCall`); the proposal kinds registry; the agent definitions that list `Propose`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | A read tool returns a kind's payload shape | merged code |

---

### Task 1: A read tool returns a kind's payload shape

**Files:** `backend/src/mlops_agents/agents/tools/` (the new tool and its registration), `agents/tools/propose.py` (its description points to the new tool); `proposals/payloads.py` (field descriptions only, where a rule is not visible from the type); the definitions under `agents/definitions/` that list `Propose` (add the tool to `tools`, one line in the prompt); tests under `backend/tests/agents/tools/`.

**Notes:**
- `ProposalShape(kind)` returns that kind's JSON schema, generated from its payload model through the kinds registry (never written by hand), so it can never drift. It is a `read` tool. Measure the `handoff` schema's size and put it in the PR: it is fetched on demand, never added to every request.
- Rules the type does not show go in the models' field descriptions, so the generated schema carries them: `on_call` may be empty when nobody is known; placeholders come from the one placeholder constant.
- `Propose`'s description says to read the kind's shape first. When `Propose` fails validation, its error ends with a pointer to `ProposalShape(kind)`.

**Tests (stop condition):** first, one that fails on `main`: `ProposalShape("connections")` and `ProposalShape("handoff")` return the schemas of their payload models (a payload valid against the model validates against the schema); an unknown kind gives the list of kinds; the on-call and placeholder rules appear in the handoff schema; the definitions that list `Propose` list the new tool; backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
