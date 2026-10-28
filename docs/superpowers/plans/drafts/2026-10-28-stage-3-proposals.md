> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Proposals Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: `docs/superpowers/plans/drafts/2026-10-28-stage-3-overview.md`.

**Goal:** the mechanism of spec §9, which onboarding's two kinds plug into:
- a `Propose` tool that is a stage 2 approval, never skipped;
- proposal records, superseding by kind and key, and the path from an approval decision to the kind's `apply()`;
- credential slots: a secret goes from the card to the keystore and never through a proposal, and no orphan stays;
- the current handoff (`Handoffs`) and its route;
- the two cards that render the payloads, the credential field and the kinds' warnings (such as the network sentence), shown as the backend gives them.

**Branch:** `plan/stage-3-proposals`. **Consumes:** I13 (the `propose` class, `kind`, `step` and `proposal_kind` on approvals, set by the gate; `Approvals.cancel`, `Approvals.add_observer`, `ApprovalObserver` with `asked(view)` and `decided(view)`, observers awaited before the turn resumes; `ApprovalControls`; the dispatcher), I19 (all models, `ConnectionsPayload`, `HandoffPayload`, `ProposalKind`, `Proposals`) and I20 as contracts writes them, "Shared by contracts" (`prp` prefix, event types, the `ProjectSecrets` instance (`put`, `delete`, `delete_prefix`, `is_set`) and the ref builders `proposal_slot_ref` and `proposal_prefix`, `handoff_versions`, the other tables), and from connections the `POST .../connections/inspect` route (I15). **Produces:** `Proposals` (I19), `ProposalKinds`, `Handoffs` (I20) from task 1 (early PR); the `Propose` tool; the routes `PUT /api/projects/{id}/proposals/{prp}/credentials/{slot}`, `GET /api/projects/{id}/proposals/by-approval/{apr}` and `GET /api/projects/{id}/handoff`; the events `proposal.created` and `proposal.decided` (`handoff.approved` is onboarding's); the two proposal cards.

**Read first:** spec §9, §7, §12; the overview's I13, I15 (`inspect`, `Findings`), I19, I20 and "Shared by contracts"; `agents/approvals/` (`gate.py`, `service.py`, `decisions.py`, `store.py`, `contract.py`, as contracts task 2 leaves them), `agents/runtime/tool_step.py` (preview runs in `ToolStep.decide` and again in `ApprovalGate.decide`, and a `ToolFailure` from it becomes the call's tool error), `agents/tools/base.py`, `credentials/project_secrets.py` (contracts: the only builder of secret refs), `frontend/src/pages/agents/ApprovalCard.tsx` and `ApprovalControls`.

**Out of scope:** each kind's `apply()`, `preview`, `warnings` and `credentials_needed` bodies, `handoff.approved`, the wizard, the kickoff, the definitions (onboarding). Here they are tested with a test kind.

## How a proposal flows

1. The agent calls `Propose(kind, payload)`. `ToolStep.decide` parses it and calls `preview`, which writes the proposal's draft (`Proposals.create`, keyed by turn and tool call ID, so the gate's second preview of the same call finds it) and returns the kind's readable text; a bad payload is a tool error and never an approval. The policy class `propose` asks at every level, `bypass` included, so the gate creates an approval and sets its `proposal_kind`.
2. The gate tells the approvals' observers (`asked(view)`). The proposals observer attaches the approval to the draft found by `(turn_id, tool_call_id)` (`Proposals.attach`), which supersedes an older pending proposal of the same kind and key (`Approvals.cancel` on the old approval). A second proposal of the same kind in the same turn step, with another tool call ID, was stored as a duplicate by `create`; its approval is decided like any other, and its `run` returns the duplicate reason as a tool error.
3. The card loads the proposal by approval, renders the payload and posts any credential to the slot route (keystore only).
4. The user decides through the stage 2 route. `decided(view)` fires on approve, reject, cancel and expiry, and every observer is awaited before the turn resumes: the observer marks the proposal, on approval runs the kind's `apply()` and stores the `ApplyResult`, and deletes the unused slots; on reject, cancel and expiry it deletes all slots.
5. On resume the approved `Propose` call runs and returns the stored `ApplyResult`; a rejected or cancelled approval never runs the tool.

## Tasks

| Task | Title | Consumes | Early PR |
|---|---|---|---|
| 1 | The proposal store and service, `ProposalKinds`, `Handoffs`, the three routes | contracts tasks 1, 2 (payloads, I20 types) and 3 (approvals behavior) | yes |
| 2 | The approval observer, superseding and restart recovery | task 1 | no |
| 3 | The `Propose` tool | tasks 1 and 2 | no |
| 4 | The shared proposal card parts and the connections card | task 1 (generated types), contracts task 3 (`ApprovalControls`, dispatcher), connections task 1 (`inspect`, `SecretFilesWarning`) | no |
| 5 | The handoff card: every section, the credential field, the warnings | task 4 | no |

Task 1 gets an early PR from `plan/stage-3-proposals-task1`: onboarding consumes `Proposals`, `ProposalKinds` and `Handoffs`. It leaves `main` working: nothing creates a proposal yet, so the routes answer 404 and `null`.

---

### Task 1: The proposal store and service, `ProposalKinds`, `Handoffs`, the three routes

**Files:**
- new `backend/src/mlops_agents/proposals/store.py` (the `proposals` table and `handoff_versions`, SQL only), `proposals/kinds.py` (`ProposalKinds`), `proposals/service.py` (`ProposalsService`), `proposals/slots.py` (slot state and sweep; secret refs come from `credentials/project_secrets.py` only), `proposals/handoffs.py` (`SqliteHandoffs`);
- new `backend/src/mlops_agents/api/routers/proposals.py` (slot route, by-approval route, handoff route; `include_routers` discovers it, no registration line);
- `api/services.py`: the `proposals`, `proposal_kinds` and `handoffs` fields and their construction (own lines only). `ProposalsService` receives the one `ProjectSecrets` instance that `build_services` already builds (contracts' line); this plan never constructs its own;
- `frontend/openapi.json` regenerated and committed (`uv run python -m mlops_agents.api.openapi_export`): CI's `openapi_export --check` fails the early PR otherwise. `schema.d.ts` is gitignored: run `npm run gen:api` locally, never commit it;
- tests under `backend/tests/proposals/` and `backend/tests/api/test_proposals_routes.py`.

**Interfaces:**
- Consumes: `ProposalView`, `ProposalStatus`, `SlotState`, `CredentialSlot`, `ApplyResult`, `ProposalKind` (with `warnings`), `Proposals`, `HandoffView`, `HandoffPayload`, `Handoffs`, `CONNECTIONS_KIND` and `HANDOFF_KIND`; the `ProjectSecrets` instance from `build_services`, injected into `ProposalsService`'s constructor (`put`, `delete`, `delete_prefix`, `is_set`), and the module ref builders `proposal_slot_ref` and `proposal_prefix`; `EventLog`; `Databases`.
- Produces: `ProposalsService` implementing `Proposals` exactly as I19 has it (`create(project_id, agent, kind, key, payload, turn_id, tool_call_id)`, `attach(project_id, proposal_id, approval_id) -> None`, `get_by_approval`, `pending`, `latest_for_turn`) plus `decide(project_id, proposal_id, approved) -> ApplyResult | None`, `for_call(project_id, turn_id, tool_call_id) -> ProposalView | None` (not in the I19 protocol; the observer and the tool use it to find their own draft) and `recover(project_id)` (used by tasks 2 and 3); `ProposalKinds` (`register`, `get`, no kind-name literals: the registered names are checked against `CONNECTIONS_KIND` and `HANDOFF_KIND` in tests; a duplicate name raises, an unknown one is a `DomainError` 422); `SqliteHandoffs`.

**Implementation notes:**
- **Store:** a `proposals` row holds the validated payload as JSON, `kind`, `key`, status, agent, `approval_id` (null until attached), `turn_id`, `tool_call_id`, `created`, `decided_at`, `result` (JSON `ApplyResult` or null). A row never holds a secret: payload models have no secret field (a test asserts it on both). Status changes are conditional (`WHERE status = 'pending'`, or `'approved'` for the result), as `ApprovalStore.update_status`. `ProposalView.warnings` is filled when a view is read from `ProposalKind.warnings(payload)` (the kind's text, as onboarding writes it); `ProposalView.slots` is filled when a view is read, from `ProjectSecrets.is_set(proposal_slot_ref(...))`, which is answered from the `secret_refs` table and never reads the keystore, never stored.
- **Concurrency:** one `asyncio.Lock` per project id serializes `create`, `attach`, `decide`, the slot route and `recover`, so a slot write racing an approval is applied before the apply (and used or swept) or refused.
- **`create(...)`:** a draft, idempotent by `(turn_id, tool_call_id)` (a second call returns the existing row, writes nothing and emits nothing). It validates `payload` with the kind's `payload_model` (a failure is `DomainError` 422, nothing stored) and inserts `pending` with a null `approval_id`. If a pending proposal of the same `kind`, `key` and `turn_id` exists under another `tool_call_id`, the new row is stored as a duplicate: status `rejected` with `result = ApplyResult(applied=[], failed={"proposal": "Another proposal of this kind was already made in this step."})`, and nothing supersedes. Emits `proposal.created {proposal_id, kind}` for a pending draft (never the payload).
- **`attach(proposal_id, approval_id)`,** under the lock: sets `approval_id` on the draft, a duplicate included (so `get_by_approval` finds its card). Only when the draft is `pending` does it then supersede any other pending proposal of the same `kind` and `key` that already has an approval (conditional update to `superseded`, sweep its slots) and, **after the lock is released**, call `Approvals.cancel` on that approval (the cancel fires `decided`, whose observer calls `decide`, which takes the same non-reentrant lock: cancelling inside it would deadlock).
- **`decide(approved)`, under the lock:** a proposal not `pending` returns `None` (a second call never applies twice). Rejection: `rejected`, sweep, emit `proposal.decided {proposal_id, kind, status}`. Approval: set `approved` first (so a slot write is refused from now on), run `kind.apply(project, proposal_id, payload)`, then in a `finally` sweep the slots (`move` already emptied the ones the kind used), store the result, emit `proposal.decided`, return it. A kind that raises is the one place a broad `except Exception` is allowed: `logger.exception`, store `ApplyResult(applied=[], failed={"apply": credentials.scrub.error_text(exc)})`, sweep. Every stored `failed` reason goes through `error_text`.
- **Sweep (`slots.sweep`):** `ProjectSecrets.delete` for each index of `kind.credentials_needed(payload)`; one function serves reject, supersede, cancel, expiry, failed apply and leftovers after a partial apply. A `KeyStoreError` is logged and added to the result as `failed["credential <i>"]`, never silent. Refs come from `proposal_slot_ref(project, proposal, slot)`; this plan writes no ref string.
- **`recover(project_id)`,** called for every project of `WorkspaceService.list_projects()` at startup (task 2 wires it; no turn runs yet): every `approved` proposal with no `result` is applied again (`apply` is idempotent, I19), then swept; `ProjectSecrets.delete_prefix(proposal_prefix(project, proposal))` removes the secrets of every non-pending proposal; every `pending` proposal with a null `approval_id` is marked `rejected` and swept.
- **Slot route** `PUT /api/projects/{project_id}/proposals/{proposal_id}/credentials/{slot}`: body `CredentialBody(value: SecretStr, min_length=1, max_length=8192, extra="forbid")`; under the lock: 404 for an unknown proposal or a slot not in `credentials_needed`, 409 `proposal_not_pending` otherwise; `ProjectSecrets.put(proposal_slot_ref(...), value)`; the response is `SlotState` (never the value). `GET .../proposals/by-approval/{apr}` returns `ProposalView` (with `slots`) or 404. Neither route logs its body.
- **`SqliteHandoffs.store(project_id, proposal_id, payload) -> int`** inserts a `handoff_versions` row (`proposal_id`, `approved = now`, payload JSON) in one transaction and returns its version; it is idempotent by proposal (the table's `UNIQUE proposal_id`): a second call for the same proposal inserts nothing and returns the same version, which onboarding's kickoff keys by. `current` returns the newest or `None`. `GET /api/projects/{project_id}/handoff` returns `HandoffView | None` with status 200.

**Tests (stop condition):**
- Service, with a test kind (two slots, scripted `apply`) and a real `ProjectSecrets` over contracts' fake keystore and a test database: `create` stores a `pending` draft with no approval and emits the event without the payload; a second `create` with the same turn and tool call ID returns the same row and emits nothing; `attach` sets the approval, and a later attach of the same kind and key supersedes the older proposal, sweeps its slots and cancels its approval after the lock is released; a different kind or key leaves it alone; a same-kind `create` in the same turn with another tool call ID is stored `rejected` with the duplicate result, and its `attach` sets the approval and supersedes nothing; `for_call` finds each by its tool call ID; approve applies once and a second `decide` returns `None` without a second apply; reject sweeps; an `apply` that moves slot 0 and fails slot 1 leaves no slot in the keystore; an `apply` that raises stores `failed["apply"]`, sweeps and does not propagate; an invalid payload is 422 and stores nothing; two concurrent decides apply once; `latest_for_turn` returns the newest of the turn and kind.
- `recover`: an approved proposal without result is applied again and swept; an orphan slot of a rejected proposal is deleted; a pending proposal's slots are kept; a pending proposal with no approval is rejected; re-applying an approved handoff proposal (test kind storing a handoff) leaves one version.
- Routes: PUT writes the keystore and returns `set: true` without the value; PUT on an approved, rejected or superseded proposal is 409 and writes nothing; an unknown slot is 404; PUT racing with approve leaves the keystore empty afterwards unless the kind moved it; the value is absent from the response, the proposal row, the events and captured logs; by-approval returns the view with `slots`, and 404 for none; `GET handoff` is `null`, then the stored one, then the newer after a second `store`, history kept.
- `ProposalKinds`: a duplicate registration raises, an unknown name is 422.
- Slot state: `slots` in a view follow `ProjectSecrets.is_set` (true after a PUT, false after a sweep), checked with the fake keystore's `get` never called by the view.
- `Handoffs.store`: storing one proposal twice returns the same version and adds no row; another proposal gets the next version; `current` is the newest.
- `warnings`: the test kind's `warnings` text appears on the by-approval view, and the proposal row stores none of it (computed when a view is read).
- `uv run ruff check`, `ruff format --check`, `pyright`, `pytest` (full suite); CI green, Windows job included (the keystore is faked).

**PR:** early PR into `main` from `plan/stage-3-proposals-task1` (`Closes #<task issue>`), as the overview's Starts column requires.

---

### Task 2: The approval observer, superseding and restart recovery

**Files:** new `backend/src/mlops_agents/proposals/observer.py` (`ProposalApprovalObserver`); `api/services.py` (own lines: build it after `ProposalsService`, `approvals.add_observer(...)`, then `recover` for every listed project); tests `backend/tests/proposals/test_observer.py`.

**Interfaces:** Consumes `ApprovalObserver`, `ApprovalView` (`id`, `turn_id`, `proposal_kind`, `status`), `Approvals.add_observer`, `Approvals.cancel`, `ProposalsService`. Produces `ProposalApprovalObserver`.

**Implementation notes:**
- Contracts owns the observer protocol, `cancel`, the gate setting `proposal_kind`, and the awaiting of observers before resume; this task implements one observer and registers it. It ignores approvals whose `proposal_kind` is null.
- `asked(view)`: finds the draft with `ProposalsService.for_call(project, view.turn_id, view.tool_call_id)` and calls `Proposals.attach(project, proposal.id, view.id)`; it reads no tool arguments and no preview text. It never cancels anything itself.
- `decided(view)`: looks the proposal up by approval and calls `ProposalsService.decide(approved=view.status == "approved")`; cancelled and expired both reject (no new status). A decision with no proposal is ignored; a duplicate's proposal is already `rejected`, so its `decided` is a no-op; every approval of a duplicate is still shown to the user and decided. The superseding cancel fires `decided` for the old approval, whose proposal is already `superseded`, so it is a no-op.
- Errors propagate: `decide` stores apply and sweep failures in the result, so an exception reaching the observer is a bug.
- The `recover` call goes last in `build_services`, after every kind registration (onboarding's lines go before it) and after the services the kinds' `apply` uses are built: it calls `ProposalKinds.get` and `apply`.

**Tests (stop condition):** through the real approvals service, gate and a fake model with a test kind: a `Propose` asks at `suggest`, `ask`, `autonomous` and `bypass` and leaves one pending proposal; approve applies before the tool result returns (observers are awaited before resume); reject, Stop (cancel) and expiry on restart each reject the proposal and empty its slots; a newer proposal of the same kind cancels the older approval (its card shows cancelled, its turn resumes) and supersedes it, also when that turn waits in another session (under a timeout, so a lock deadlock fails instead of hanging); a non-propose approval never reaches the proposals service; a restart with an approved proposal that has no result applies it once. Checks as task 1; CI green.

---

### Task 3: The `Propose` tool

**Files:** new `backend/src/mlops_agents/agents/tools/propose.py`; `agents/tools/__init__.py` (`default_registry` gains its own keyword and lines); `api/services.py` (own line); tests `backend/tests/agents/tools/test_propose.py`.

**Interfaces:** Consumes `ToolContext.tool_call_id` (contracts), `RiskClass.PROPOSE`, `ProposalKinds`, `Proposals.create`, `ProposalsService.for_call`, `ConnectionsPayload`, `HandoffPayload`, `ApplyResult`, `CONNECTIONS_KIND` and `HANDOFF_KIND` of `proposals/contract.py`. Produces `ProposeTool` (name `Propose`, risk `propose`) and `PROPOSE_TOOL_NAME`.

```python
class ProposeArgs(BaseModel):   # extra="forbid"
    kind: str    # a field validator accepts only CONNECTIONS_KIND or HANDOFF_KIND (Literal cannot take names under pyright strict); its error names both
    payload: ConnectionsPayload | HandoffPayload
    # the gate reads `kind` to set the approval's proposal_kind
```

**Implementation notes:**
- A validator rejects a payload whose class is not the kind's payload model. A test asserts the union members equal the payload models named by the contract's kind constants (the registry is onboarding's to fill), so a new kind that forgets the union fails a test instead of the agent.
- `preview(ctx, args)` writes the draft and nothing else: `Proposals.create(ctx.project_id, ctx.agent, kind, key=ctx.project_id, payload, ctx.turn_id, ctx.tool_call_id)` (idempotent, so the gate's second preview finds it), and returns the kind's own `ProposalKind.preview(payload)` text with the title `Proposal: <kind>`. A `DomainError` from `create` (an invalid payload) and an unknown kind become a `ToolFailure`, which `ToolStep.decide` turns into the call's tool error before any approval exists. It never raises for the second preview of one call: `ApprovalGate.decide` does not catch a `ToolFailure` from preview.
- `run(ctx, args)` reads its own proposal with `ProposalsService.for_call(project, ctx.turn_id, ctx.tool_call_id)`, never the newest of the kind. A duplicate's stored `failed` reason comes back as a tool error (the approval for a duplicate is decided like any other: on approve the agent reads that reason, on reject the standard rejection text).
- Otherwise `run` is reached only after approval and returns the stored result: "Approved. Applied: a, b. Failed: x: reason." or "Approved. Everything was applied."; `is_error` when `failed` is non-empty; with no stored result, an error saying the state could not be read, never a guess. The tool writes nothing.

**Tests (stop condition):** valid connections and handoff payloads parse; kind/payload mismatch and unknown fields are tool errors naming the field; a payload with a `bypass` grant is refused by the handoff model before any record or approval exists (the model is contracts'); two previews of one call leave one draft and one event; a second `Propose` of the same kind in one step with another tool call ID is a tool error for that call and its approval is cancelled, while the first proceeds; two same-kind calls in one step, both approved: the first reports its apply result, the second the duplicate error; through the real gate and graph with a fake model: the call pauses at every level including `bypass`, approve resumes with the result text, reject resumes with the rejection, a failed part gives `is_error` naming the part; two steps of one turn proposing the same kind each read their own result (by tool call ID); after a full propose, post-credential, approve cycle the credential value appears in none of the approvals, audit records, events or the checkpoint (a test searches all four). Checks as task 1; CI green.

---

### Task 4: The shared proposal card parts and the connections card

**Files:** new `frontend/src/pages/agents/proposals/useProposal.ts` (TanStack Query over the by-approval route), `ProposalShell.tsx` (title, status badge, loading and error fallback showing `preview.body` text, result lines, the proposal's `warnings` as given; `ApprovalControls` keyed on the approval part's status, not the proposal's, with the proposal's failure reason shown above them), `ConnectionsProposalCard.tsx`, tests; one line in contracts' dispatcher in `ApprovalCard.tsx` (`proposal_kind` `connections`).

**Interfaces:** Consumes connections' `SecretFilesWarning`, the generated `ProposalView`, `ConnectionsPayload`, `Findings` and the by-approval and `POST /api/projects/{id}/connections/inspect` routes, `ApprovalControls`, the dispatcher. Produces `ProposalShell` and `useProposal` (task 5 reuses them).

**Implementation notes:**
- Types from the generated `schema.d.ts` only. The typed payload comes from the route; no JSON is parsed in the card.
- The connections card lists each connection: name, kind, its settings as a key/value list (text only), and the reason. While the proposal is pending, the card calls `inspect` once per connection (kind and settings) and passes the returned `Findings` to connections' `SecretFilesWarning`, the one component that shows findings (imported, never copied; the wording is the backend's `Finding.reason`, and this card adds none of its own). An `inspect` failure shows its message on that connection and does not block the decision. `inspect` creates nothing, so the card calls it freely; the call is made by the card, never by the proposals plan's backend.
- A decided card shows the status and, when approved, the applied and failed parts from `result`; superseded and cancelled cards show their status with no controls and no `inspect` call. Reuse `ApprovalControls` and the stage 2 status badge; copy nothing. The Inbox needs no change: it already renders `ApprovalCard`.
- Strict TypeScript, no `any`.

**Tests (stop condition):** stubbed routes as `ApprovalCard.test.tsx` does: every connection renders with its reason; each connection's `inspect` result is handed to `SecretFilesWarning` (asserted by rendering the real component with a secret-file finding and an unsupported-file finding, with a distinctive `Finding.reason` string in the stub, asserted verbatim in the card, so the test proves the wording is the backend's and fails if the card stops using the component); an `inspect` error shows on its connection and the decision still posts; a pending card posts the decision with the note; a failed proposal load shows the fallback preview and still lets the user decide; a decided card shows status, applied and failed parts, no controls and no `inspect` call; a duplicate (proposal `rejected`, approval `pending`) shows its reason above working Approve and Reject controls; a superseded card shows its status; the dispatcher renders this card for `proposal_kind` `connections` and the old card for other tools. `npm test`, lint, format check, build; CI green. No e2e here (onboarding's flow covers it).

---

### Task 5: The handoff card: every section, the credential field, the warnings

**Files:** new `frontend/src/pages/agents/proposals/HandoffProposalCard.tsx`, `HandoffSections.tsx` (one small component per section, to keep files short), `CredentialField.tsx`, tests; one line in the dispatcher (`proposal_kind` `handoff`).

**Interfaces:** Consumes `ProposalShell`, `useProposal`, `ProposalView.slots` and `warnings`, the slot route and the generated `HandoffPayload` types (I19: `models`, `commands`, `data_roles`, `tracking`, `pipeline`, `edges`, `operations`, `gaps`, `grants`). Produces nothing others use.

**Implementation notes:**
- **Every section of the payload renders**, in this order, from the typed payload:
  - **Models:** per `ModelSpec`: name; profile (objective, problem type, target and positive class, primary metric and its rationale, operating threshold, label delay and maturity days, serving mode, error costs and cost ratio, groups); predictions and labels table references; its commands; promotion (`team_gate` or `person`); environments (tracker alias to environment); production signal.
  - **Commands** (project-level) and each model's commands, as a table: kind, type, the template or the workflow, inputs, placeholders, whether it deploys, entry files.
  - **Data roles:** table (connection, table, role, id column, time column).
  - **Tracking:** type, settings (URL or workflows), auth.
  - **Pipeline:** stages in order (name, entry, inputs, outputs) and edges as "from to to".
  - **Operations:** scheduler (name, config location); feeds (name, kind, reference, cadence, grace, timezone); on-call (name, contact, channel, hours, timezone, escalation order); checks (`check_id`, name, category, cadence, timezone, where results land: file connection and pattern, CI workflow, or MLflow metric and threshold); results location.
  - **Gaps:** what and the next step.
  - **Grants:** network on or off, permission level (the type excludes `bypass`).
  A cadence renders as its cron or "every N minutes". A null or empty section renders "none".
- **Credential field**, one per entry of `slots`: label from the slot, `<input type="password" autoComplete="off">`; Save posts `{value}` to the slot route and clears local state at once (also on error); then it shows only "credential set: yes" or "no" from the response and the refetched view; never the value or a mask of it. Save is disabled when empty or when the proposal is not pending. The value lives only in component state, never in the query cache, a URL or the console.
- **Warnings:** `ProposalView.warnings` renders as given, above the controls, one line each, with no frontend text of its own (the network sentence for an MLflow source with `auth: "none"` is onboarding's `ProposalKind.warnings`; there is no `networkSentence.ts`). No warnings, no block.
- Approving with a slot unset is allowed and says so next to the field: "the source is created without a credential; add one under Tracking sources in the project settings" (the settings section is the pages plan's).

**Tests (stop condition):** a full-payload fixture (two models, every command kind, feeds, on-call, all three result kinds, gaps, grants) renders every field of every section, found by label, and a minimal payload renders the empty sections as "none"; the credential field posts the value once and clears it, and the value is found nowhere in the DOM or the query cache afterwards; a failed save shows the error and still clears the field; Save is disabled for a non-pending proposal; `warnings` render as given above the controls and nothing renders for an empty list; a decided card shows `result`. `npm test`, lint, format check, build; the whole plan: backend suite, ruff, pyright, `npm run e2e` still green on the existing flows; CI green on every job.
