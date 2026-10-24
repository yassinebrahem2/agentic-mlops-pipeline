# Stage 2 — Runtime Contracts Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Land, once, everything the stage 2 runtime plans share, so each of them only adds its own module:
- the types and protocols between them;
- the whole stage 2 schema;
- the turn graph's hook points.

Nothing here adds behavior a user can see. Every default keeps stage 1's behavior until the plan that owns it replaces the default.

**Branch:** `plan/stage-2-runtime-contracts`. **Consumes:** merged code; task 3 also needs the tool-errors plan (#145) merged, because both edit `graph.py` and `model_caller.py`. **Produces:** I8 to I12 (stage 2 overview, Interfaces between plans).

**Read first:** the stage 2 spec (`docs/superpowers/specs/2026-10-24-stage-2-agent-runtime-design.md`), all of it; the overview's interface section.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Shared types and protocols | merged code |
| 2 | The stage 2 schema | task 1 |
| 3 | Turn graph hook points and per-call tool context | task 1, tool-errors plan merged |

Every task gets an early PR (its issue says so): every runtime plan waits on them.

---

### Task 1: Shared types and protocols

**Files:**
- `backend/src/mlops_agents/agents/tools/base.py`: `TaskRef`, `ToolContext` v2, `Preview`, `AgentTool.parse` and `AgentTool.preview`. Only these; the tool-errors plan edits `schema()` in the same file. Bring `main` in first if it has merged.
- `backend/src/mlops_agents/agents/runtime/contract.py`: the I1 additions.
- New `backend/src/mlops_agents/agents/policy/__init__.py` and `table.py`.
- New `backend/src/mlops_agents/events/__init__.py` and `contract.py`.
- New `backend/src/mlops_agents/agents/tasks/__init__.py` and `contract.py`.
- New `backend/src/mlops_agents/sandbox/__init__.py` and `contract.py`.
- New `backend/src/mlops_agents/agents/approvals/__init__.py` and `contract.py`.
- New `backend/src/mlops_agents/agents/messaging/__init__.py` and `contract.py`.
- New `backend/src/mlops_agents/agents/runtime/hooks.py` (the protocols and dataclasses only; the defaults come in task 3).
- `backend/src/mlops_agents/workspace/refs.py`: `github_remote`.
- `backend/src/mlops_agents/agents/catalog.py`: the `status_board` field.
- `frontend/openapi.json` and `frontend/src/api/schema.d.ts`, regenerated.
- The tests of each.

**Interfaces:** Consumes merged code. Produces I8 to I12 exactly as the overview writes them. The overview is the one source of their names and types; this task writes them into code without changing them. A needed change takes a Decision issue.

**Implementation notes:**
- `AgentTool.invoke` becomes `parse` then `run`, as the overview says. Behavior is unchanged, and stage 1's tests pass as they are.
- `AgentTool.preview` has a default: kind `text`, the tool name as title, the arguments as indented JSON as body.
- `policy/table.py` holds the spec's §5 table as data, plus `decide(risk, level)`. Nothing else in the code base may encode a policy value.
- `github_remote(project, origin_url)` returns `RepoRef(owner, repo)` for a GitHub project (from `github_full_name`). For a local project it parses `origin_url` with the existing `_PATTERNS`, returning `None` when the URL is not github.com. `https_url(ref)` builds `https://github.com/<owner>/<repo>.git`. Reading the local `origin` URL is the tasks plan's job, which passes it in.
- The context builder (`runtime/context.py`) and the hub's fold (`hub.py`) skip `ApprovalPart` when building the model's history; the frontend's `MessageView` ignores it until the frontend-agents plan renders it. Name every reader of `Part` in the PR: backend `context.py`, `compaction.py`, `hub.py`, and frontend `MessageView.tsx`, `applyEvent.ts`.

**Tests (stop condition):**
- The policy table: one test per cell (24), against the spec's table.
- `parse`: valid arguments pass; invalid ones give the stage 1 error text; `invoke` behaves as before.
- `github_remote`: GitHub project; local project with an https origin, an SSH origin, a non-GitHub origin, and no origin.
- The I1 types: an approval part and a `waiting` message round-trip through `MessageView`, and the context builder skips the approval part.
- A definition with `status_board: true` loads; one with an unknown key still fails.
- `npm run gen:api` leaves no diff after the commit, and `npm test` and the frontend build pass.
- Full backend suite, ruff, format, pyright; CI green.

---

### Task 2: The stage 2 schema

**Files:** new `backend/src/mlops_agents/storage/migrations/project/0002_stage2.sql`, `backend/src/mlops_agents/storage/tables.py` (the new tables and columns), `backend/src/mlops_agents/agents/runtime/store.py` (the `messages` columns it reads and writes), tests.

**Interfaces:** Consumes task 1. Produces the schema below. It is the only stage 2 migration: later plans add none without a Decision issue.

**Schema** (project.db; every `id` is the roadmap's prefixed ULID, every time UTC ISO 8601):
- `tasks`:
  - `id`, `session_id` (FK sessions, cascade), `agent`, `title`, `branch`, `worktree`;
  - `base_ref`, `base_sha`;
  - `status` CHECK in `active`, `in_review`, `merged`, `closed`, `discarded`;
  - `review_kind` CHECK in `github`, `local` or NULL;
  - `pr_number`, `pr_url`;
  - `created_at`, `ended_at`.
  - Index on (`session_id`, `status`).
- `task_reads`: `task_id` (FK tasks, cascade), `path`, `sha256`, PK (`task_id`, `path`).
- `file_claims`: `task_id` (FK tasks, cascade), `path`, PK (`task_id`, `path`); index on `path`.
- `approvals`:
  - `id`, `turn_id` (FK turns, cascade), `session_id`, `agent`, `tool_call_id` UNIQUE;
  - `tool`, `risk_class`, `preview` (JSON);
  - `status` CHECK in `pending`, `approved`, `rejected`, `cancelled`, `expired`;
  - `note`, `created_at`, `decided_at`.
  - Index on `status`.
- `events`: `seq` INTEGER PK AUTOINCREMENT, `id` UNIQUE, `ts`, `source`, `type`, `subject_id`, `payload` (JSON).
- `audit`: `id` PK, `ts`, `actor`, `action`, `risk_class`, `permission_level`, `decision`, `target`, `detail` (JSON).
- `exchanges`: `id` PK, `created_at`.
- `wakeups`:
  - `id`, `session_id` (FK sessions, cascade), `agent`, `note`, `due_at`;
  - `status` CHECK in `pending`, `fired`, `cancelled`;
  - `created_at`, `fired_at`.
  - Index on (`status`, `due_at`).
- `messages`, rebuilt (SQLite cannot alter a CHECK):
  - its `status` CHECK gains `waiting`;
  - new columns `exchange_id` (nullable), `hop` (nullable integer), `read_by_turn` (nullable, FK turns, SET NULL).
  - The rebuild keeps every row, `seq` values and indexes, inside the migration's transaction. Foreign keys are checked after it (`PRAGMA foreign_key_check` returns nothing).

**Tests (stop condition):**
- A project.db at version 1 with sessions, turns and messages migrates to 2 with the same rows and `seq` values. The backup is taken first (stage 1's runner).
- Every CHECK rejects a bad value, and every cascade works.
- A fresh project.db gets both migrations.
- Full backend suite, ruff, format, pyright; CI green.

---

### Task 3: Turn graph hook points and per-call tool context

**Files:** `backend/src/mlops_agents/agents/runtime/graph.py`, `executor.py`, `agent.py`, `hooks.py` (the four defaults), `sessions.py` (building the executor and graph, the observers, `post`), `backend/src/mlops_agents/api/services.py` (wiring the defaults), tests.

**Interfaces:** Consumes task 1 and the merged tool-errors plan. Produces I12's hook points.

**Implementation notes:**
- The graph becomes model → decide → wait → tools → model:
  - `decide` calls `Gate.decide` and stores its result in the state (`decisions: dict[str, GateDecision]`).
  - `wait` calls `Gate.wait` only when a decision is `ask`.
  - `tools` runs the calls whose decision is `allow` or `approved`, and answers the others with the decision's tool error.
  - After the model ends a turn, the graph asks `TurnEndCheck.note(...)` once. When it returns a note, one more model step runs with it.
  - Before each model call, the graph appends `InboundSource.take(...)`.
- The defaults keep stage 1's behavior. Each lives in its protocol's module, next to the protocol:
  - `AllowAllGate`;
  - `NoInbound`;
  - `NoTurnEndCheck`;
  - `RepositoryOnlyWorkspaces`: no task; the repository's `RepoFiles`.

  The plan that owns a protocol deletes its default when it wires the real one.
- The tools step builds a `ToolContext` for each call, from `TaskWorkspaces.current` and `TaskWorkspaces.files`. `Agent.tool_context` (stage 1, one per turn) is removed.
- Tool arguments are parsed (`AgentTool.parse`) before `decide`. A call that fails to parse is answered with its error and never reaches the gate.
- `SessionManager` gains `post` (I9). `send` becomes a call to the same internal path, so both store a message and a queued turn one way. It also takes an optional `MessageBus` and calls `bind(self)` on it, and a sequence of `TurnObserver`s that it calls when a turn starts, waits and ends.
- `services.py` builds the registry with `default_registry(...)`, taking the services tools need (none yet), and passes the four defaults to the session manager. Later plans each change their one line there.

**Tests (stop condition):**
- With the defaults, every stage 1 runtime test passes unchanged.
- A test gate that denies one call: that call gets the denial as its error result, and the others run. A test gate that asks: `wait` is reached, and with a test `wait` that approves, the call runs.
- A test inbound source's messages appear in the next model request, after the previous step's tool results.
- A test turn-end check's note causes exactly one more model step, and none when it returns `None`.
- A call with invalid arguments never reaches the test gate.
- `post` stores an inbound message (`author_kind` user, `from_agent` set, `exchange_id` and `hop` stored), starts a turn when idle, and queues one when busy, exactly like `send`.
- A test observer sees `turn_started` and `turn_ended`, with the final text, for a turn.
- Two tool calls in one step, with a test `TaskWorkspaces` whose current task changes after the first: the second call's context sees the change.
- Full backend suite, ruff, format, pyright; CI green on Linux and Windows.
