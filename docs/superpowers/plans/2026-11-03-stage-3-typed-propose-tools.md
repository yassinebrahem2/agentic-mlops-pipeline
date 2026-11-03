# Stage 3: One Typed Propose Tool per Kind Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Two tasks in order, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** bug #727. `Propose(kind, payload)` declares `payload` with no type, and in the real onboarding request the model sends it as JSON text every time (lead's replay of walkthrough 2 through the app's own code: 6 of 6 tries, streaming or not, 11,631 prompt tokens as the real call). The same request with a typed `ProposeConnections` tool (the payload's fields as the tool's own parameters) gave 3 of 3 valid objects at 418 fewer tokens; with no `ProposalShape` call in the history it gave 3 of 3 objects, 1 of 3 valid: two names broke a rule the schema states only as a regex. Typing `payload` as a bare object made it worse (0 of 6 in a short probe). So each registered proposal kind becomes its own tool, `ProposalShape` goes, and the fields a model gets wrong get plain-word descriptions.

**Branch:** `plan/stage-3-typed-propose-tools`. **Milestone:** Stage 3: Onboarding and data access. **Consumes:** merged code. **Produces:** the `Propose<Kind>` tools (the stage 3 and 4-6 overviews' `Propose` lines, changed with this plan). The plan PR says `Closes` the plan issue, its task issues and #727.

**Read first:** bug #727; `backend/src/mlops_agents/agents/tools/propose.py`, `proposal_shape.py`, `tools/__init__.py` (the registry), `tools/base.py` (`schema`, `parse`, `_clean_schema`); `agents/approvals/gate.py` (`_proposal_kind`, `PROPOSE_BYPASS_OVERRIDES` in `agents/policy/actions.py`); `proposals/kinds.py`, `proposals/contract.py`, `proposals/payloads.py`; `monitoring/expr.py` (`Condition`, `MAX_CONDITION_DEPTH`); the agent definitions that list `Propose`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | One typed tool per proposal kind, beside `Propose` | merged code |
| 2 | Agents, prompts and fakes use their kinds' tools; `Propose` and `ProposalShape` go | task 1 |

---

### Task 1: One typed tool per proposal kind, beside `Propose`

**Files:** `backend/src/mlops_agents/agents/tools/propose.py` (the new tool beside today's, its name rule), `tools/__init__.py` (one tool per kind), `tools/base.py` (only if the schema step needs it); `proposals/contract.py` (the tuple of kinds); `agents/approvals/gate.py` (`_proposal_kind`); `monitoring/expr.py` (the bounded condition schema); the payload models, docstrings and field descriptions only: `proposals/payloads.py` (`ConnectionsPayload`, `HandoffPayload`), `monitoring/hooks/contract.py` (`HooksPayload`), `monitoring/results/contract.py` (`ParserPayload`), `monitoring/rules/contract.py` (`RulePayload`), `monitoring/incidents/contract.py` (`MaintenancePayload`), `promotion/contract.py` (`PromotionPayload`), `training/contract.py` (`TrainingPolicyPayload`); tests under `backend/tests/agents/tools/` and `backend/tests/agents/`.

**Notes:**
- One tool class, one instance per kind. The tools and the agent catalog are built before the kinds are registered (`api/services.py`), so the kinds come from one tuple of the kind constants in `proposals/contract.py`, and each tool looks up its kind object and payload model in the registry when it is used (`schema`, `parse`, `preview`). Its arguments are that kind's payload model (validated by it, so every rule, `ProposalCheck` included, stays where it is); its name is `Propose` followed by the kind in PascalCase (`training_policy` gives `ProposeTrainingPolicy`), one function in `propose.py`. Preview, `create`, the duplicate refusal and `run` behave as today's `Propose`. Today's `Propose` stays until task 2, so the definitions keep loading.
- The tool carries its kind; the gate takes `proposal_kind` from the tool when it carries one (from the arguments only for today's `Propose`, until task 2), so `PROPOSE_BYPASS_OVERRIDES` (promotion is allowed at `bypass`) keeps applying.
- Each tool's description is its payload model's docstring: one or two sentences, what is proposed and that a person approves it; it is sent once, as the tool's description (pydantic also writes a docstring into the schema's top-level `description`, which the tool drops). The parser contract and the `ran_at` text that `Propose.schema()` appends today go with the parser kind's tool only.
- Schemas stay self-contained (no `$ref`: not every provider resolves it, `_clean_schema`). A `Condition` field is sent as a bounded schema generated from `expr.py`'s models: a comparison, or `all_of`, `any_of` or `not_` over comparisons (`TOOL_CONDITION_DEPTH = 2` beside `MAX_CONDITION_DEPTH`). Validation still uses the payload model, which accepts up to `MAX_CONDITION_DEPTH`.
- Plain-word descriptions on the fields models fill wrong or blind, at least: a connection's `name` (lowercase letters, digits and dashes, starting with a letter or digit, at most 40), a local folder's `path` (the folder on the person's machine as they gave it, never the sandbox's `/data/<name>`); audit the other payload fields a model fills and add one where the type alone does not say it.
- An invalid call answers with every error's field path and message; a value that arrived as text where an object or list belongs says so.

**Tests (stop condition):** first, one that fails on `main`: a `ProposeConnections` call with the connections object creates a pending proposal; the tuple of kinds and the registered kinds are the same set, and each has exactly one tool, named by the rule; every tool's schema has no `$ref` and a valid payload of each kind's model (a two-level condition included) validates against it (`jsonschema`); a promotion call at `bypass` is still allowed and any other kind still asks; a bad connection `name` gets an error naming `connections.0.name`; text where an object belongs gets an error saying text arrived; backend suite, ruff, pyright; CI green.

**Review of task 1 (2026-11-03, on 5fe34d0c).** Fix on the branch before the plan PR (with task 2 or before it); `backend/src/mlops_agents/connections/contract.py` is added to task 1's files (`LocalFolderSettings` lives there; the plan named the wrong file):
- The host-folder rule belongs on the leaf: `LocalFolderSettings.path` still reaches the model as a bare string, the hint sits on `ConnectionSpec.settings`. Put it on `path`, and a test reads it in `ProposeConnections`' schema.
- Field descriptions where a model fills blind: `RulePayload`'s fields (`check_id`, `window_minutes`, `quantifier`, `min_records`, the spec's `name`, `why`, `hook_id`, `hook_kind`, `limits`) and `HandoffPayload`'s top-level fields; a description that only restates the type ("The mitigation rule specification.") does not count.
- A test ties `PROPOSAL_KINDS` to the kinds the app really registers (the services built as at startup, not a mock).
- `TOOL_CONDITION_DEPTH` drives the bounded schema, or goes: today the bounded types are hand-written and the constant is unused.

### Task 2: Agents, prompts and fakes use their kinds' tools; `Propose` and `ProposalShape` go

**Files:** `backend/src/mlops_agents/agents/definitions/` (onboarding, data-quality, deployment, model-performance, retraining); `agents/tools/proposal_shape.py`, today's `Propose` and `ProposeArgs`, and their registry entries (removed); `agents/approvals/gate.py` (the arguments fallback goes); `onboarding/handoff_preview.py` (its docstring); every test that names `Propose`, `ProposeTool`, `ProposeArgs`, `PROPOSE_TOOL_NAME` or `ProposalShape`: `backend/tests/conftest.py`, `backend/tests/fakes/` (`onboarding_script.py`, `monitoring_script.py`, `test_onboarding_script.py`), `backend/tests/e2e/` (`test_harness.py`), `backend/tests/agents/` (`gate_helpers.py`, `test_approval_gate.py`, `test_approval_propose_path.py`, `test_approval_store_stall.py`, `test_approvals_stall_and_cancel.py`, `test_gate_actions.py`, `test_catalog.py`, `test_agent_definitions.py`, `tools/test_propose.py`, `tools/test_proposal_shape.py` (removed), `tools/test_register_check.py`), `backend/tests/proposals/` (`test_observer.py`, `test_restart_repair.py`), `backend/tests/promotion/test_promotion_kind.py`, `backend/tests/onboarding/test_propose_live.py`, `backend/tests/api/test_agents_routes.py`, `frontend/src/pages/inbox/InboxPage.test.tsx`, `frontend/src/pages/agents/proposals/PromotionProposalCard.test.tsx`.

**Notes:**
- Each definition lists exactly the tools of the kinds its prompt proposes: onboarding connections, handoff, hooks, parser; data-quality maintenance, parser; deployment hooks, rule; model-performance rule; retraining promotion, training policy. Prompts name the tool (`ProposeConnections`), not "a `connections` proposal with `Propose`"; the `ProposalShape` lines go.
- A turn waiting on a `Propose` approval across the upgrade ends that call with the existing "There is no tool named Propose." tool error (`tool_step.py`); the agent proposes again.
- Onboarding carries `ProposeHandoff` on every turn, so a handoff can be proposed again (the stage 3 spec: a newer approved proposal replaces the current one).

**Tests (stop condition):** first, one that fails on `main`: each definition's propose tools are exactly its kinds' (a test over the five definitions); no definition, prompt or tool names `Propose` alone or `ProposalShape`; the fake-provider onboarding and monitoring scripts and the e2e suites pass with the new calls; backend suite, frontend tests, `npm run e2e`, ruff, pyright; CI green.

## Known debt added

- `ProposeHandoff`'s schema (about 13,000 characters) is sent on every onboarding call, approved handoff or not; measured with hooks at 4,437 prompt tokens per call. Fix when a request limit bites: offer it only while onboarding proposes a handoff.
- Agents can propose conditions two levels deep; the store accepts four (`MAX_CONDITION_DEPTH`). No real condition so far nests at all.
