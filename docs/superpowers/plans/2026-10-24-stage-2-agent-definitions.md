# Stage 2 — Agent Definitions Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** All seven agents exist as definition files on the one runtime, with prompts that describe only what they can do now (spec §9).

**Branch:** `plan/stage-2-agent-definitions`. **Consumes:** every stage 2 tool on `main` (tasks, sandbox, github-work and messaging merged). **Produces:** the seven definitions.

**Read first:** spec §9; `project_knowledge.md` (Agents); the stage 1 `orchestrator.md`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The Orchestrator and the coding agents | every stage 2 tool on `main` |
| 2 | Alert Prioritization | task 1 |

---

### Task 1: The Orchestrator and the coding agents

**Files:** `backend/src/mlops_agents/agents/definitions/orchestrator.md`, new `onboarding.md`, `data-quality.md`, `model-performance.md`, `retraining.md` and `deployment.md` in the same folder, tests.

**Interfaces:** Consumes the tool names on `main`. Produces six chat definitions.

**Implementation notes:**
- Tools, `can_message` and `schedules`: exactly spec §9's table. The Orchestrator has `status_board: true`.
- Every prompt has three parts:
  1. **The role**, from `project_knowledge.md`.
  2. **What the agent can do now:**
     - read and change code through tasks;
     - run commands in the sandbox (git does not work there);
     - open PRs and issues;
     - message the agents it may message.

     It also says what is not available yet (data connections, checks, deploy hooks) and says so when asked. Stage 1's "cannot change files" lines go.
  3. **How to work:**
     - StartTask before changing code, and ResumeTask to return to one;
     - Read before Edit;
     - test in the sandbox;
     - OpenPullRequest when the work is finished, or say what you wait for;
     - SearchIssues before OpenIssue, and comment on a match instead;
     - message another agent only when its part is needed, and keep messages short and specific.
- Prompts are plain and short. Each stays under about 60 lines.
- Read stays the first tool of every definition. The fake model `fake-tools` calls an agent's first tool, and the stage 1 e2e flow and runtime tests rely on it being Read.

**Tests (stop condition):**
- The catalog loads all definitions.
- Each one's tools and `can_message` equal the spec's table, through one parametrized test reading the table as data.
- Only the Orchestrator has `status_board`.
- A live test (`-m live`, not in CI) asks the Orchestrator to add a line to the README on a temporary GitHub-less project at level `autonomous`. It starts a task, edits, and opens a local review. Run it once and state the result in the PR.
- Full backend suite, checks, and `npm run build && npm run e2e`; CI green, E2E included.

---

### Task 2: Alert Prioritization

**Files:** new `backend/src/mlops_agents/agents/definitions/alert-prioritization.md`, tests.

**Interfaces:** Consumes task 1. Produces the seventh definition.

**Implementation notes:**
- `chat: false`, with the tools of spec §9's table. `can_message` holds every chat agent. No agent lists it in its own `can_message` in stage 2.
- Its prompt describes deduplicating, ranking and routing alerts, and says it is started by the monitoring loop (stage 4).

**Tests (stop condition):** it loads; it is `inactive` on the status board; sending it a chat message gets stage 1's `agent_cannot_chat`; no definition may message it; full suite, checks; CI green.
