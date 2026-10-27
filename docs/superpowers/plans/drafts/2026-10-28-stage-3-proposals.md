> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Proposals Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: `docs/superpowers/plans/drafts/2026-10-28-stage-3-overview.md`.

**Goal:** the mechanism of spec §9, which onboarding's two kinds plug into:
- a `Propose` tool that is a stage 2 approval, never skipped;
- proposal records, superseding, and the path from an approval decision to the kind's `apply()`;
- credential slots: a secret goes from the card to the keystore and never through a proposal, and no orphan stays;
- the current handoff (`Handoffs`) and its route;
- the two cards that render the payloads, the credential field and the network sentence.

**Branch:** `plan/stage-3-proposals`. **Consumes:** I13 (the `propose` class, `kind`, `step` and `proposal_kind` on approvals, `Approvals.cancel`, `Approvals.add_observer`, `ApprovalObserver`, `ApprovalControls`, the dispatcher), I19 and I20 as models and protocols, "Shared by contracts" (`prp` prefix, event types, `project_secrets`, `handoff_versions` and the other tables), all from contracts tasks 1 and 2. **Produces:** `Proposals` (I19), `ProposalKinds`, `Handoffs` (I20) from task 1 (early PR); the `Propose` tool; the routes `PUT /api/projects/{id}/proposals/{prp}/credentials/{slot}`, `GET /api/projects/{id}/proposals/by-approval/{apr}` and `GET /api/projects/{id}/handoff`; the events `proposal.created` and `proposal.decided` (`handoff.approved` is onboarding's); the two proposal cards.

**Read first:** spec §9, §7 (the network sentence), §12; the overview's I13, I19, I20 and "Shared by contracts"; `agents/approvals/` (`gate.py`, `service.py`, `decisions.py`, `store.py`, `contract.py`, as contracts task 2 leaves them), `agents/runtime/tool_step.py` (preview runs twice per call, in `ToolStep.decide` and `ApprovalGate.decide`: it must be pure), `agents/tools/base.py`, `credentials/project_secrets.py` (contracts), `frontend/src/pages/agents/ApprovalCard.tsx` and `ApprovalControls`.

**Out of scope:** each kind's `apply()` body, its `preview` and `credentials_needed` bodies, `handoff.approved`, the wizard, the kickoff, the definitions (onboarding). Here they are tested with a test kind.

## How a proposal flows

1. The agent calls `Propose(kind, payload)`. `ToolStep.decide` parses and previews it (a bad kind or payload is a tool error, never an approval). The policy class `propose` asks at every level, `bypass` included, so the gate creates an approval.
2. The gate tells the approvals' observers (`asked(view)`). The proposals observer creates the proposal (`Proposals.create`, `pending`) and, if an older pending proposal of the same kind exists, supersedes it (`Approvals.cancel` on the old approval).
3. The card (`proposal_kind` on the approval part) loads the proposal by approval, renders the payload and posts any credential to the slot route (keystore only).
4. The user decides through the stage 2 route. `decided(view)` fires on approve, reject, cancel and expiry: the observer marks the proposal, on approval runs the kind's `apply()`, stores the `ApplyResult` and deletes the unused slots; on reject, cancel and expiry it deletes all slots.
5. On resume the approved `Propose` call runs and returns the stored `ApplyResult`; a rejected or cancelled approval never runs the tool.

## Tasks

| Task | Title | Consumes | Early PR |
|---|---|---|---|
| 1 | The proposal store and service, `ProposalKinds`, `Handoffs`, the three routes | contracts tasks 1 and 2 | yes |
| 2 | The approval observer, superseding and restart recovery | task 1 | no |
| 3 | The `Propose` tool | tasks 1 and 2 | no |
| 4 | The shared proposal card parts and the connections card | task 1 (generated types), contracts task 2 (`ApprovalControls`, dispatcher) | no |
| 5 | The handoff card: credential field and network sentence | task 4 | no |

Task 1 gets an early PR from `plan/stage-3-proposals-task1`: onboarding consumes `Proposals`, `ProposalKinds` and `Handoffs`. It leaves `main` working: nothing creates a proposal yet, so the routes answer 404 and `null`.

---

### Task 1: The proposal store and service, `ProposalKinds`, `Handoffs`, the three routes

**Files:**
- new `backend/src/mlops_agents/proposals/store.py` (the `proposals` table and `handoff_versions`, SQL only), `proposals/kinds.py` (`ProposalKinds`), `proposals/service.py` (`ProposalsService`), `proposals/slots.py` (slot refs, state, sweep), `proposals/handoffs.py` (`SqliteHandoffs`);
- new `backend/src/mlops_agents/api/routers/proposals.py` (slot route, by-approval route, handoff route), registered by one line;
- `api/services.py`: the `proposals`, `proposal_kinds` and `handoffs` fields and their construction (own lines only);
- `frontend/openapi.json` and `frontend/src/api/schema.d.ts` regenerated (`uv run python -m mlops_agents.api.openapi_export`, `npm run gen:api`): CI's `openapi_export --check` fails the early PR otherwise;
- tests under `backend/tests/proposals/` and `backend/tests/api/test_proposals_routes.py`.

**Interfaces:**
- Consumes: `ProposalView`, `ProposalStatus`, `SlotState`, `CredentialSlot`, `ApplyResult`, `ProposalKind`, `Proposals`, `HandoffView`, `HandoffPayload`, `Handoffs`; `project_secrets` (`put`, `move`, `delete`, `delete_prefix`); `EventLog`; `Databases`.
- Produces: `ProposalsService` implementing `Proposals` (`create`, `get_by_approval`, `pending`) plus `decide(project_id, proposal_id, approved) -> ApplyResult | None` and `recover(project_id)` (used by task 2); `ProposalKinds` (`register`, `get`; a duplicate name raises, an unknown one is a `DomainError` 422); `SqliteHandoffs`.

**Implementation notes:**
- **Store:** a `proposals` row holds the validated payload as JSON, status, agent, `approval_id`, `turn_id`, `created`, `decided_at`, `result` (JSON `ApplyResult` or null). A row never holds a secret: payload models have no secret field (a test asserts it on both models). Status changes are conditional (`WHERE status = 'pending'`, or `'approved'` for the result), as `ApprovalStore.update_status`. `ProposalView.slots` is filled when a view is read, from `KeyStore.get(ref) is not None` through `asyncio.to_thread` in `slots.py` (a read; `project_secrets` stays the only writer), never stored.
- **Concurrency:** one `asyncio.Lock` per project id serializes `create`, `decide`, the slot route and `recover`, so a slot write racing an approval is either applied before the apply (and used or swept) or refused.
- **`create(project, agent, kind, payload, approval_id, turn_id)`:** validate `payload` with the kind's `payload_model` (a failure is `DomainError` 422, nothing stored); a pending proposal of the same kind with the same `turn_id` raises `DomainError("proposal_duplicate_in_step")` (G4); insert `pending`; supersede an older pending proposal of the same kind: inside the lock only the conditional update to `superseded` and the sweep of its slots; **after the lock is released**, `Approvals.cancel` on its approval (that cancel fires `decided`, whose observer calls `decide`, which takes the same non-reentrant lock: cancelling inside it would deadlock); emit `proposal.created {proposal_id, kind, approval_id}` (never the payload).
- **`decide(approved)`, under the lock:** a proposal not `pending` returns `None` (a second call never applies twice). Rejection: `rejected`, sweep, emit `proposal.decided {proposal_id, kind, status}`. Approval: set `approved` first (so a slot write is refused from now on), run `kind.apply(project, proposal_id, payload)`, then in a `finally` sweep the slots (`move` already emptied the ones the kind used), store the result, emit `proposal.decided`, return it. A kind that raises is the one place a broad `except Exception` is allowed: `logger.exception`, store `ApplyResult(applied=[], failed={"apply": credentials.scrub.error_text(exc)})`, sweep. Every stored `failed` reason goes through `error_text`.
- **Sweep (`slots.sweep`):** `project_secrets.delete` for each index of `kind.credentials_needed(payload)`; one function serves reject, supersede, cancel, expiry, failed apply and leftovers after a partial apply. A `KeyStoreError` is logged and added to the result as `failed["credential <i>"]`, never silent. Slot refs are `project/<id>/proposal/<prp>/<slot>`, built in `slots.py` only.
- **`recover(project_id)`,** run at startup for every project of `WorkspaceService.list_projects()` by one call in `build_services` (task 2 wires it; the method itself is here): every `approved` proposal with no `result` is applied again (`apply` is idempotent, I19), then swept; for every non-pending proposal `project_secrets.delete_prefix("project/<id>/proposal/<prp>/")` removes orphans left by a crash.
- **Slot route** `PUT /api/projects/{project_id}/proposals/{proposal_id}/credentials/{slot}`: body `CredentialBody(value: SecretStr, min_length=1, max_length=8192, extra="forbid")`; under the lock: 404 for an unknown proposal or a slot not in `credentials_needed`, 409 `proposal_not_pending` otherwise; `project_secrets.put`; the response is `SlotState` (never the value). `GET .../proposals/by-approval/{apr}` returns `ProposalView` (with `slots`) or 404. Neither route logs its body.
- **`SqliteHandoffs.store`** inserts a `handoff_versions` row (`proposal_id`, `approved = now`, payload JSON) in one transaction; `current` returns the newest or `None`. `GET /api/projects/{project_id}/handoff` returns `HandoffView | None` with status 200.

**Tests (stop condition):**
- Service, with a test kind (two slots, scripted `apply`) and contracts' fake keystore: create stores `pending` and emits the event without the payload; a second create of the same kind supersedes the first, sweeps its slots and cancels its approval; a create of another kind leaves the first alone; a same-kind create in the same turn raises `proposal_duplicate_in_step`; approve applies once and a second `decide` returns `None` without a second apply; reject sweeps; an `apply` that moves slot 0 and fails slot 1 leaves no slot in the keystore; an `apply` that raises stores `failed["apply"]`, sweeps and does not propagate; an invalid payload is 422 and stores nothing; two concurrent decides apply once.
- `recover`: an approved proposal without result is applied again and swept; an orphan slot of a rejected proposal is deleted; a pending proposal's slots are kept.
- Routes: PUT writes the keystore and returns `set: true` without the value; PUT on an approved, rejected or superseded proposal is 409 and writes nothing; an unknown slot is 404; PUT racing with approve leaves the keystore empty afterwards unless the kind moved it; the value is absent from the response, the proposal row, the events and captured logs; by-approval returns the view with `slots`, and 404 for none; `GET handoff` is `null`, then the stored one, then the newer after a second `store`, history kept.
- `ProposalKinds`: a duplicate registration raises, an unknown name is 422.
- `uv run ruff check`, `ruff format --check`, `pyright`, `pytest` (full suite); CI green, Windows job included (the keystore is faked).

**PR:** early PR into `main` from `plan/stage-3-proposals-task1` (`Closes #<task issue>`), as the overview's Starts column requires.

---

### Task 2: The approval observer, superseding and restart recovery

**Files:** new `backend/src/mlops_agents/proposals/observer.py` (`ProposalApprovalObserver`); `api/services.py` (own lines: build it after `ProposalsService`, `approvals.add_observer(...)`, then `recover` for every listed project); tests `backend/tests/proposals/test_observer.py`.

**Interfaces:** Consumes `ApprovalObserver`, `ApprovalView` (`kind`, `proposal_kind`, `turn_id`, `agent`, `preview`, `status`), `Approvals.add_observer`, `Approvals.cancel`, `ProposalsService`. Produces `ProposalApprovalObserver`.

**Implementation notes:**
- Contracts owns the observer protocol, `cancel`, and the firing of `asked` and `decided`; this task only implements an observer and registers it. It ignores approvals whose `proposal_kind` is null.
- `asked`: calls `Proposals.create(project, view.agent, args.kind, args.payload, view.id, view.turn_id)` with the tool args. This task is blocked until G1 gives `asked` the args or the payload; nothing is parsed out of preview text.
- `decided(view)`: looks the proposal up by approval and calls `ProposalsService.decide(approved=view.status == "approved")`; cancelled and expired both reject (no new status). A decision with no proposal is ignored. The superseding cancel fires `decided` for the old approval, whose proposal is already `superseded`, so it is a no-op.
- The `recover` call goes last in `build_services`, after every kind registration (onboarding's lines go before it) and after the services the kinds' `apply` uses are built: it calls `ProposalKinds.get` and `apply`, and an unregistered kind would fail startup.
- Errors propagate: `decide` stores apply and sweep failures in the result, so an exception reaching the observer is a bug.

**Tests (stop condition):** through the real approvals service, gate and a fake model with a test kind: a `Propose` asks at `suggest`, `ask`, `autonomous` and `bypass` and creates one pending proposal; approve applies before the tool result returns; reject, Stop (cancel) and expiry on restart each reject the proposal and empty its slots; a newer proposal of the same kind cancels the older approval (its card shows cancelled, its turn resumes) and supersedes it, also when that turn waits in another session (this test runs through the real approvals service under a timeout, so a lock deadlock fails instead of hanging); two same-kind `Propose` calls in one model message give the second a tool error and leave one pending proposal; two different kinds both ask; a non-propose approval never reaches the proposals service. Checks as task 1; CI green.

---

### Task 3: The `Propose` tool

**Files:** new `backend/src/mlops_agents/agents/tools/propose.py`; `agents/tools/__init__.py` (`default_registry` gains its own keyword and lines); `api/services.py` (own line); tests `backend/tests/agents/tools/test_propose.py`.

**Interfaces:** Consumes `RiskClass.PROPOSE`, `ProposalKinds`, `Proposals`, `ConnectionsPayload`, `HandoffPayload`, `ApplyResult`, the kind-name constants of `proposals/contract.py`. Produces `ProposeTool` (name `Propose`, risk `propose`) and `PROPOSE_TOOL_NAME`.

```python
class ProposeArgs(BaseModel):   # extra="forbid"
    kind: Literal["connections", "handoff"]            # the contract's constants
    payload: ConnectionsPayload | HandoffPayload
```

**Implementation notes:**
- A validator rejects a payload whose class is not the kind's payload model. A test asserts the union members equal the payload models named by the contract's kind constants (the registry is onboarding's to fill), so a new kind that forgets the union fails a test instead of the agent.
- `preview(ctx, args)` is pure and cheap: the kind's own `ProposalKind.preview(payload)` with the title `Proposal: <kind>`, deterministic, so the Inbox and the card's fallback read plain text. The card shows the typed payload from the route. An unknown kind raises `ToolFailure`.
- `ProposeArgs` exposes `proposal_kind` (a property returning `kind`) for the gate to record on the approval (G2).
- `run(ctx, args)`, reached only after approval, returns the stored result of the latest approved proposal of this turn and kind (`Proposals.latest_for_turn`, G3): "Approved. Applied: a, b. Failed: x: reason." or "Approved. Everything was applied."; `is_error` when `failed` is non-empty; if no result is stored, an error saying the state could not be read, never a guess. The tool writes nothing.

**Tests (stop condition):** valid connections and handoff payloads parse; kind/payload mismatch and unknown fields are tool errors naming the field; a payload with a `bypass` grant is refused by the handoff model before any approval exists (the model is contracts'); `preview` is pure and equal across calls; through the real gate and graph with a fake model: the call pauses at every level including `bypass`, approve resumes with the result text, reject resumes with the rejection, a failed part gives `is_error` naming the part; two steps of one turn proposing the same kind each read their own result; after a full propose, post-credential, approve cycle the credential value appears in none of the approvals, audit records, events or the checkpoint (a test searches all four). Checks as task 1; CI green.

---

### Task 4: The shared proposal card parts and the connections card

**Files:** new `frontend/src/pages/agents/proposals/useProposal.ts` (TanStack Query over the by-approval route), `ProposalShell.tsx` (title, status badge, loading and error fallback showing `preview.body` text, result lines, `ApprovalControls` when pending), `ConnectionsProposalCard.tsx`, tests; one line in contracts' dispatcher in `ApprovalCard.tsx` (`proposal_kind` `connections`).

**Interfaces:** Consumes the generated `ProposalView` and the by-approval route, `ApprovalControls`, the dispatcher. Produces `ProposalShell` and `useProposal` (task 5 reuses them).

**Implementation notes:**
- Types from the generated `schema.d.ts` only. The typed payload comes from the route; no JSON is parsed in the card.
- The connections card lists each connection: name, kind, its settings as a key/value list (text only), and the reason.
- A decided card shows the status and, when approved, the applied and failed parts from `result`; superseded and cancelled cards show their status with no controls. Reuse `ApprovalControls` and the stage 2 status badge; copy nothing. The Inbox needs no change: it already renders `ApprovalCard`.
- Strict TypeScript, no `any`.

**Tests (stop condition):** stubbed routes as `ApprovalCard.test.tsx` does: every connection renders with its reason; a pending card posts the decision with the note; a failed load shows the fallback preview and still lets the user decide; a decided card shows status, applied and failed parts and no controls; a superseded card shows its status; the dispatcher renders this card for `proposal_kind` `connections` and the old card for other tools. `npm test`, lint, format check, build; CI green. No e2e here (onboarding's flow covers it).

---

### Task 5: The handoff card: credential field and network sentence

**Files:** new `frontend/src/pages/agents/proposals/HandoffProposalCard.tsx`, `CredentialField.tsx`, `networkSentence.ts` (one function, the one source of the text), tests; one line in the dispatcher (`proposal_kind` `handoff`).

**Interfaces:** Consumes `ProposalShell`, `useProposal`, `ProposalView.slots`, the slot route and the generated `HandoffPayload` types (field names are contracts', from spec §9: profile, data roles, tracking, pipeline, commands, operations, gaps, grants). Produces nothing others use.

**Implementation notes:**
- Sections in spec order: profile; data roles as a table (table, role, id column, time column); tracking sources (type, URL or workflows, auth); pipeline stages in order; commands; operations (scheduler, checks with `check_id`, name, category, cadence, where results land); gaps with next steps; grants.
- **Credential field**, one per entry of `slots`: label from the slot, `<input type="password" autoComplete="off">`; Save posts `{value}` to the slot route and clears local state at once (also on error); then it shows only "credential set: yes" or "no" from the response and the refetched view; never the value or a mask of it. Save is disabled when empty or when the proposal is not pending. The value lives only in component state, never in the query cache, a URL or the console.
- **Network sentence:** when the network grant is on and a tracking source has `auth: "none"`, one sentence per such source above the controls: "Network access lets agents reach <url>, which has no authentication: anyone who can reach it can change it, agents included at autonomous and bypass levels; every command is in the audit log." None when the network is off or every source uses a credential.
- The permission level grant never offers `bypass`.
- Approving with a slot unset is allowed and says so next to the field ("the source is created without a credential; add one in settings").

**Tests (stop condition):** every section renders from a full fixture; the credential field posts the value once and clears it, and the value is found nowhere in the DOM or the query cache afterwards; a failed save shows the error and still clears the field; Save is disabled for a non-pending proposal; the sentence shows for network on with an `auth: "none"` source (its URL named) and not for network off or credentialed sources; a decided card shows `result`. `npm test`, lint, format check, build; the whole plan: backend suite, ruff, pyright, `npm run e2e` still green on the existing flows; CI green on every job.

---

## Interface gaps

Still open after the overview's revision; each needs a Decision or an overview edit before the task that uses it.

- **G1: payload source for `asked`.** `ApprovalObserver.asked(view)` carries no tool args, but `Proposals.create` needs the payload, and I19 gives no other path. Needed: `asked(view, args)` (or the payload on the view). Task 2 waits on it. Parsing the preview text is not acceptable as a workaround.
- **G2: setting `proposal_kind`.** I13 adds `proposal_kind` to approvals but names no one who sets it. Needed in contracts' gate: take it from the tool args (`args.proposal_kind` when present).
- **G3: lookup for `Propose.run`.** `ToolContext` has no tool-call or approval ID, so the tool cannot use `get_by_approval`. Needed: `Proposals.latest_for_turn(project_id, turn_id, kind) -> ProposalView | None` in I19 (or the call ID on `ToolContext`).
- **G4: two same-kind proposals in one model step.** I19 says the second gets a tool error but not where. This plan raises `proposal_duplicate_in_step` from `create` inside `asked`; contracts' gate must turn an observer's `DomainError` into a `deny` for that call only, and close the approval it just created (`Approvals.cancel`, without resuming the turn, which is still deciding), or that turn would wait on it.
- **G5: order of `decided` and resume.** `Propose.run` needs the stored result, so every `decided` observer must be awaited before `resume_turn` on all paths (decision route, Stop, expiry); I13 does not say so.
