> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Proposals Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: `docs/superpowers/plans/drafts/2026-10-28-stage-3-overview.md`.

**Goal:** the mechanism of spec §9, which onboarding's two kinds plug into:
- a `Propose` tool that is a stage 2 approval, never skipped;
- proposal records, superseding, and the path from an approval decision to the kind's `apply()`;
- credential slots: a secret goes from the card to the keystore and never through a proposal, and no orphan stays;
- the current handoff (`Handoffs`) and its route;
- the two cards that render the payloads, the credential field and the network sentence.

**Branch:** `plan/stage-3-proposals`. **Consumes:** I13 (the `propose` class, `kind` on approvals), I19 and I20 as models and protocols from contracts, the approval-card dispatcher (contracts task 2), the stage 3 tables (contracts task 1). **Produces:** `Proposals` (I19) and `Handoffs` (I20) from task 1 (early PR), the `Propose` tool, `credentials/project_secrets.py`, the routes `PUT /api/projects/{id}/proposals/{prp}/credentials/{slot}` and `GET /api/projects/{id}/handoff`, the events `proposal.created`, `proposal.decided`.

**Read first:** spec §9, §7 (the network sentence), §12; the overview's I13, I19, I20; `agents/approvals/` (`gate.py`, `service.py`, `decisions.py`, `store.py`, `contract.py`), `agents/runtime/tool_step.py` (preview is called twice, once in `ToolStep.decide`, once in `ApprovalGate.decide`: it must be pure), `agents/tools/base.py` (`AgentTool.preview`, `Preview`), `credentials/keystore.py`, `frontend/src/pages/agents/ApprovalCard.tsx`.

**Out of scope (onboarding's):** each kind's `apply()` body, the wizard, the kickoff, the definitions. This plan ships the registry kinds plug into and tests it with a test kind.

## How a proposal flows

1. The agent calls `Propose(kind, payload)`. `ToolStep.decide` parses and previews it (a bad kind or payload is a tool error, never an approval). `ApprovalGate.decide` sees risk class `propose`, so policy says `ask`, and creates the approval.
2. The gate then tells its **approval observers** (`asked`). The proposals observer creates the proposal record (`Proposals.create`, status `pending`), supersedes an older pending proposal of the same kind, and emits `proposal.created`.
3. The card (pending approval, tool `Propose`) loads the proposal, renders the payload, and posts any credential to the slot route (keystore only).
4. The user approves or rejects through the stage 2 route. Every approval status change (approve, reject, Stop's cancel, restart's expiry) goes through `record_decisions`, which tells its observers (`decided`). The proposals observer calls `Proposals.decide(approved)`: on approve it runs the kind's `apply()`, stores the `ApplyResult` on the record, and sweeps the unused credentials. This happens before the turn resumes.
5. On resume the approved `Propose` call runs: `Propose.run` reads the stored `ApplyResult` and returns what was applied and what failed. A rejected, cancelled or superseded approval never runs the tool; the gate's rejection text (with the note) is the result.

## Tasks

| Task | Title | Consumes | Early PR |
|---|---|---|---|
| 1 | Credentials helper, the proposal store and service, kinds registry, `Handoffs`, the three routes | contracts tasks 1 and 2 on `main` | yes |
| 2 | Approval observers: from an approval decision to `Proposals` | task 1, contracts task 2 | no |
| 3 | The `Propose` tool | tasks 1 and 2, contracts task 2 | no |
| 4 | The proposal card and the connections card | task 1 (types), contracts task 2 (dispatcher) | no |
| 5 | The handoff card: credential field and network sentence | task 4 | no |

Task 1 gets an early PR from `plan/stage-3-proposals-task1`: onboarding's kinds and tracking's credential route consume `Proposals`, `Handoffs` and `project_secrets`. Its PR leaves `main` working: nothing yet creates a proposal, the routes answer 404 and `null`.

---

### Task 1: Credentials helper, the proposal store and service, kinds registry, `Handoffs`, routes

**Files:**
- new `backend/src/mlops_agents/credentials/project_secrets.py`;
- new `backend/src/mlops_agents/proposals/store.py` (the `proposals` and handoff tables, SQL only), `proposals/kinds.py` (`ProposalKinds` registry), `proposals/service.py` (`ProposalsService`), `proposals/handoffs.py` (`SqliteHandoffs`), `proposals/credentials.py` (slot logic);
- new `backend/src/mlops_agents/api/routers/proposals.py` (slot route, read route, handoff route), registered by one line in the router list;
- `api/services.py`: `proposals`, `handoffs`, `proposal_kinds` fields and their construction (own lines only);
- `ids.py` (`prp` in `PREFIXES`) and `events/contract.py` (the event types) are contracts' (G8); this plan only uses them;
- `frontend/openapi.json` and `frontend/src/api/schema.d.ts`, regenerated (`uv run python -m mlops_agents.api.openapi_export`, then `npm run gen:api`): CI's `openapi_export --check` fails the early PR otherwise;
- tests: `backend/tests/credentials/test_project_secrets.py`, `backend/tests/proposals/test_service.py`, `test_credentials.py`, `test_handoffs.py`, `backend/tests/api/test_proposals_routes.py`.

**Interfaces:**
- Consumes: `ProposalView`, `ProposalStatus`, `ProposalKind`, `CredentialSlot`, `ApplyResult`, `Proposals`, `HandoffView`, `HandoffPayload`, `Handoffs` (I19, I20, contracts task 1); `KeyStore`; `EventLog`; `Databases`.
- Produces: `Proposals` and `Handoffs` implementations; `ProposalKinds` (see Interface gaps G5); `project_secrets` functions:

```python
def proposal_ref(project_id: str, proposal_id: str, slot: int) -> str    # project/<id>/proposal/<prp>/<slot>
def tracking_ref(project_id: str, source_id: str) -> str                 # project/<id>/tracking/<trk>
async def put(keystore: KeyStore, ref: str, value: str) -> None
async def get(keystore: KeyStore, ref: str) -> str | None
async def move(keystore: KeyStore, src: str, dst: str) -> bool            # False when src is empty; src is deleted only after dst is written
async def delete(keystore: KeyStore, ref: str) -> None                   # idempotent
async def is_set(keystore: KeyStore, ref: str) -> bool
```

**Implementation notes:**
- **`project_secrets` is the only place that writes project credentials** (spec §9, I19). Every call goes through `asyncio.to_thread` (the keyring is blocking, as in `providers/keys.py`). `move` is `get`, `set dst`, `delete src`, in that order, so a crash leaves a duplicate (swept later), never a loss. Errors are `KeyStoreError`, whose messages never hold a key. A ref is built only by these helpers; no other module formats one.
- **Store:** `proposals` rows hold the validated payload as JSON (no secret can be in it: kinds' payload models have no secret field, and a test asserts it), status, agent, `approval_id`, `turn_id`, `created`, `decided_at`, `result` (JSON `ApplyResult` or null). `ProposalRecord(ProposalView)` adds `turn_id`, `result`, as `ApprovalRecord` extends `ApprovalView`. Status changes are conditional (`WHERE status = 'pending'`), as `ApprovalStore.update_status`.
- **`ProposalsService.create`** (inside the project's proposal lock, one `asyncio.Lock` per project id): validate `payload` with the kind's `payload_model`; insert `pending`; find an older `pending` proposal of the same kind and call the supersede path (below); emit `proposal.created {proposal_id, kind, approval_id}` (no payload in the event).
- **Superseding** (`_supersede(old)`): conditional update to `superseded`; sweep its credential slots (below); ask the approvals side to cancel the old approval with the note "Superseded by a newer proposal." through the `ApprovalCanceller` of task 2, so the old card shows cancelled and its waiting turn resumes with that note as the tool error. A `Proposals.pending(project, kind)` returns the one pending proposal or `None`.
- **`ProposalsService.decide(project, proposal_id, approved)`**, under the project lock:
  - not `pending` → return `None` (decided twice is a no-op, never a second apply);
  - `approved=False` → status `rejected`, sweep the slots, emit `proposal.decided {proposal_id, kind, status}`, return `None`;
  - `approved=True` → status `approved` first (so a slot write racing in is refused), then `kind.apply(...)`; always in `finally` sweep the slots (`move` already emptied the ones the kind used); store the result. A kind that raises (not returning an `ApplyResult`) is the one place a broad `except Exception` is allowed: log with `logger.exception`, store `ApplyResult(applied=[], failed={"apply": error_text(exc)})`, and still sweep. Return the result.
  - The `failed` reasons pass through `credentials.scrub.error_text` before they are stored, so a reason never quotes a secret.
- **Slot sweep** (`proposals/credentials.py`, one function `sweep(proposal)`): for each slot index in `kind.credentials_needed(payload)` call `project_secrets.delete(proposal_ref(...))`. This single function serves reject, supersede, failed apply and leftovers after a partial apply. A `KeyStoreError` during the sweep is logged and recorded on the proposal as `failed["credential <i>"]` so it is never silent.
- **Slot route** `PUT /api/projects/{project_id}/proposals/{proposal_id}/credentials/{slot}`: body `CredentialBody(value: SecretStr, min_length=1, max_length=8192, extra="forbid")`; under the project lock, 404 for an unknown proposal or a slot not in `credentials_needed`, 409 `proposal_not_pending` otherwise; writes with `project_secrets.put`; the response is `CredentialState(index: int, set: bool)` and never the value. `GET .../proposals/by-approval/{approval_id}` returns `ProposalDetail` (G2): the `ProposalView` plus `slots: list[CredentialSlotState]` (`index`, `label`, `source_type`, `set`) read from the keystore. Neither route logs its body; a test with a log capture checks the value appears nowhere, in `repr` of the request model included (`SecretStr` hides it).
- **`SqliteHandoffs`**: `store` inserts a new version row (`proposal_id`, `approved = now`, payload JSON) in one transaction; `current` returns the newest or `None`; a later `store` replaces it as current and keeps history. `GET /api/projects/{project_id}/handoff` returns `HandoffView` or `null` (response model `HandoffView | None`).
- `ProposalKinds`: `register(kind: ProposalKind)` (duplicate name raises, as `ToolRegistry`), `get(name)`, `names()`. Onboarding registers its two kinds in `api/services.py` with its own lines.

**Tests (stop condition):**
- `project_secrets` against an in-memory `KeyStore` fake: refs have the documented shape; `move` leaves `dst` set and `src` gone, returns `False` on a missing src, and keeps `src` when writing `dst` fails; `delete` is idempotent; a `KeyStoreError` message never contains the value.
- Service, with a test kind (payload model with two slots, scripted `apply`): create stores `pending` and emits the event without the payload; a second create of the same kind supersedes the first, sweeps its slots and asks for its approval's cancel; a create of another kind leaves the first alone; approve applies once and returns the result; a second `decide` returns `None` without a second apply; reject sweeps; an `apply` that moves slot 0 and fails slot 1 leaves no slot in the keystore; an `apply` that raises stores `failed["apply"]`, sweeps and does not propagate; a payload that fails validation is a `DomainError` 422 and stores nothing; two concurrent decides apply once.
- Routes: PUT writes the keystore and returns `set: true` without the value; PUT on a decided, rejected or superseded proposal is 409 and writes nothing; PUT to an unknown slot is 404; PUT racing with approve either lands before the apply (and is used or swept) or is refused, never left behind (assert the keystore is empty afterwards); the value is absent from the response, the proposal row, the events and the captured logs; `GET handoff` is `null`, then the stored one, then the newer after a second `store`.
- Handoffs: version history kept, `current` is the latest.
- `uv run ruff check`, `ruff format --check`, `pyright`, `pytest` (full suite); CI green, Windows job included (the keystore is faked, so no keyring dependence).

**PR:** early PR into `main` from `plan/stage-3-proposals-task1` (`Closes #<task issue>`), as the overview's Starts column requires.

---

### Task 2: Approval observers: from an approval decision to `Proposals`

**Files:** `backend/src/mlops_agents/agents/approvals/contract.py` (the `ApprovalObserver` and `ApprovalCanceller` protocols, G1), `agents/approvals/gate.py` (calls `asked`), `agents/approvals/decisions.py` (`record_decisions` calls `decided`), `agents/approvals/service.py`, `agents/runtime/waiting.py`, `agents/runtime/store/__init__.py` (the three callers of `record_decisions` pass the observers), new `backend/src/mlops_agents/proposals/observer.py` (`ProposalApprovalObserver`), `api/services.py` (own lines: build the observer, pass it to the gate, the approvals service and the session manager, and bind the canceller), tests.

**Interfaces:**
- Consumes: `Proposals`, `ProposalKinds`, `ApprovalRecord`, `ApprovalStore`, `DecidedApprovalStatus`.
- Produces:

```python
class ApprovalObserver(Protocol):
    async def asked(self, project_id: str, approval: ApprovalView, args: BaseModel) -> None: ...
    async def decided(self, project_id: str, approval: ApprovalView, status: DecidedApprovalStatus) -> None: ...
class ApprovalCanceller(Protocol):
    async def cancel(self, project_id: str, approval_id: str, note: str) -> None: ...   # a pending approval becomes cancelled with the note; its turn resumes
```

**Implementation notes:**
- Approvals never import `proposals`: they only call the protocol. `ProposalApprovalObserver` ignores every approval whose `tool != "Propose"` (the name is one constant, exported by the tool's module).
- **Two same-kind `Propose` calls in one model message:** the proposals observer cannot supersede a proposal whose approval belongs to the turn that is still deciding (that turn is running, not waiting, and the first approval's part is not written yet). `Propose.preview` cannot see it, so `asked` refuses: `Proposals.create` raises `DomainError("proposal_duplicate_in_step", ...)` when a pending proposal of the same kind has the same `turn_id`, and the gate turns it into a tool error for the second call only (G9). Supersede (and `cancel`) therefore only ever target a proposal of another turn, or of an earlier step of the same turn that is already waiting or ended.
- `asked` is called by the gate after `store.create` and the `approval.requested` event, with the args the gate already holds (no second parse). It calls `Proposals.create(project, agent, args.kind, args.payload, approval.id)`; the proposal row stores `turn_id` from the approval.
- `decided` is called by `record_decisions` for every status it records, so Stop (`cancelled`) and restart (`expired`) are covered with no other code path. It looks the proposal up by `approval_id` and calls `Proposals.decide(approved=status == "approved")`; cancelled and expired map to a rejection (the proposal status is `rejected`; no new status). An approval with no proposal (decided before `asked` ran, or another tool) is ignored.
- `record_decisions` calls observers after the audit record and the event and catches nothing: an apply failure never raises out of `Proposals.decide` (it is stored as `failed["apply"]`, a sweep failure as `failed["credential <i>"]`), so an exception that does escape is a bug and propagates. The decision is already recorded (status, audit, event) when it does.
- `ApprovalCanceller` is implemented by `ApprovalsService.cancel` (new method): conditional `update_status(..., "cancelled", note=...)`, updates the message part, `record_decisions(..., "cancelled")` with `actor="system"` (which calls `decided`; the already-superseded proposal is not `pending`, so it is a no-op), then resumes the turn when no approval of it is pending, like `decide`. Its audit record is the standard cancelled record.
- Constructor wiring: the gate, `ApprovalsService` and `SessionManager` each take `observers: Sequence[ApprovalObserver] = ()`; the observer is built after `Proposals`, so `api/services.py` builds it before the gate and binds `Proposals` to it late (`bind`), as `inbound.bind(sessions)` does.

**Tests (stop condition):**
- Gate: a `Propose` call at `suggest`, `ask`, `autonomous` and `bypass` always yields an `ask` decision (the policy row itself is contracts'; this asserts the end-to-end behavior) and calls `asked` once with the approval and the args; a non-propose tool does not reach the proposals observer; a gate with no observers behaves as before.
- Service: approve calls `decided("approved")` before `resume_turn` (record the call order with a spy); reject calls it with `rejected`; the stop path (`waiting.py`) and the expiry path (`runtime/store`) call it with `cancelled` and `expired`, and each rejects the proposal and sweeps its credential (use task 1's service with a test kind and a fake keystore).
- An observer that raises on `decided` propagates its error (nothing swallowed) after the approval status, audit record and event were written; a kind whose `apply` raises does not reach this case (task 1 stores it).
- `cancel`: the approval becomes `cancelled` with the note, its part shows it, the turn resumes with the note in the tool error; superseding a proposal whose turn is waiting in another session cancels it and resumes that turn; two same-kind `Propose` calls in one message give the second a tool error and one pending proposal; two different kinds in one message both ask.
- Existing approval tests unchanged and green.
- Checks as task 1; CI green.

---

### Task 3: The `Propose` tool

**Files:** new `backend/src/mlops_agents/agents/tools/propose.py`, `agents/tools/__init__.py` (`default_registry` gains one `proposals`/`kinds` keyword and its own lines), `api/services.py` (own line), tests `backend/tests/agents/tools/test_propose.py`.

**Interfaces:**
- Consumes: `RiskClass.PROPOSE` (contracts task 2), `ProposalKinds`, `Proposals`, `ConnectionsPayload`, `HandoffPayload`, `ApplyResult`.
- Produces: `ProposeTool` (name `Propose`, risk `propose`), `PROPOSE_TOOL_NAME`.

```python
class ProposeArgs(BaseModel):   # extra="forbid"
    kind: Literal["connections", "handoff"]            # the names are the constants of the proposals contract
    payload: ConnectionsPayload | HandoffPayload
```

**Implementation notes:**
- A model validator on `ProposeArgs` rejects a payload whose class is not the kind's `payload_model` ("kind handoff needs a handoff payload"). A test asserts the union members equal the payload models named by contracts' kind-name constants (not the registry, which onboarding fills), so a new kind that forgets the union fails a test instead of the agent.
- `preview(ctx, args)` must be pure and cheap (it runs twice per call): returns `Preview(kind="text", title=f"Proposal: {kind}", body=kind.preview(payload).body)`. The body is the kind's readable text; it holds no credential (only "credential set: yes/no" is never in it either: the card reads the live state from the route). An unknown kind raises `ToolFailure`.
- `run(ctx, args)` is reached only after approval. It reads the latest approved proposal for `ctx.turn_id` and `args.kind` (`Proposals`-side lookup, G2; the duplicate-in-step refusal makes it unique within a step, and a turn that proposes the same kind in two steps gets the newest, which a test pins) and returns its stored `ApplyResult` as text: "Approved. Applied: a, b. Failed: x: reason." or "Approved. Everything was applied." The tool error flag is set when `failed` is non-empty so the agent reacts. If no stored result exists (the `decided` observer failed), return an error saying the proposal's state could not be read, never a guess.
- The tool never writes: all effects are in `Proposals.decide`.
- Risk class `propose` asks at every level by the policy table (contracts); this task adds no policy code.

**Tests (stop condition):**
- Two steps of one turn proposing the same kind: each run reads its own result (the older one was decided before the newer is asked).
- Args: a valid connections and a valid handoff payload parse; a kind/payload mismatch and an unknown field are tool errors naming the field; a payload with a `bypass` permission grant is refused by the handoff model's own validation (spec §9: a proposal can never set `bypass`; the model is contracts', this asserts it reaches the agent as a tool error before any approval exists).
- `preview` is pure: two calls give equal output and write nothing.
- Through the real gate and graph with a fake model: the call pauses at every permission level including `bypass`; approve resumes with the result text; reject resumes with the rejection and the note; a result with a failed part returns `is_error` and names the part.
- After a full propose, post-credential, approve cycle with a test kind, the credential value appears nowhere in the approval, audit, events or the checkpoint (a test that searches those four stores for it).
- Checks as task 1; CI green.

---

### Task 4: The proposal card and the connections card

**Files:** new `frontend/src/pages/agents/proposals/ProposalCard.tsx` (loads the proposal, wraps the decision, dispatches on `proposal.kind`), `ConnectionsProposalCard.tsx`, `useProposal.ts` (TanStack Query hook over the by-approval route), tests; one line in contracts' dispatcher in `frontend/src/pages/agents/ApprovalCard.tsx` (tool `Propose` renders `ProposalCard`). The generated types already hold the routes (task 1 regenerated them).

**Interfaces:** Consumes `ProposalDetail` and the decision route from the generated types only (no hand-written copies); the decision controls (note, Approve, Reject) of the dispatcher refactor (G6). Produces `ProposalCard`.

**Implementation notes:**
- The approval's `preview.body` is a fallback shown while the proposal loads or if it fails to load; the typed payload comes from the route, so no JSON is parsed out of text.
- The connections card lists each connection: name, kind, its settings as a key/value list (values escaped, never as HTML), and the reason. A decided card shows the status badge and, when approved, the applied and failed parts from `result`.
- Reuse the stage 2 card's decision controls, status badge and `DiffView` neighbors; do not copy them (AGENTS.md DRY). The Inbox already renders `ApprovalCard`, so proposal cards appear there with no Inbox change.
- A superseded or cancelled proposal renders its status and note, with no buttons.
- Strict TypeScript, no `any`; handles loading and error states with the project's existing patterns.

**Tests (stop condition):** Vitest with stubbed routes (as `ApprovalCard.test.tsx` does): the card renders every connection with its reason; a pending card posts the decision with the note; a loading failure shows the fallback preview and still lets the user decide; a decided card shows status, applied and failed parts and no buttons; a superseded card shows its note; the dispatcher renders `ProposalCard` for `Propose` and the old card for other tools. `npm test`, `npm run lint`, `npm run format:check`, `npm run build`; CI green. No e2e here (onboarding's flow covers the full path).

---

### Task 5: The handoff card: credential field and network sentence

**Files:** new `frontend/src/pages/agents/proposals/HandoffProposalCard.tsx`, `CredentialField.tsx`, `networkSentence.ts` (one function, its text the single source), tests; one line in `ProposalCard.tsx`'s kind switch.

**Interfaces:** Consumes `ProposalDetail.slots`, the slot route, the generated `HandoffPayload` types (field names are contracts', from spec §9: profile, data roles, tracking, pipeline, commands, operations, gaps, grants). Produces nothing others use.

**Implementation notes:**
- Sections in spec order: profile; data roles as a table (table, role, id column, time column); tracking sources (type, URL or workflows, auth); pipeline stages in order; commands; operations (scheduler, checks with `check_id`, name, category, cadence, where results land); gaps with their next steps; grants (network on or off, permission level).
- **Credential field** (`CredentialField`, one per slot of `slots`): label from the slot, `<input type="password" autoComplete="off">`, Save posts `{value}` to the slot route then clears local state at once (also on error); shows only "credential set: yes" or "no" from the route's response and the loaded slots, never the value, never a mask of it; Save is disabled when empty or when the proposal is not pending. The value is held only in component state, never in the query cache, a URL or the console.
- **Network sentence:** when `grants.network` is on and any tracking source has `auth: "none"`, a warning above the buttons: "Network access lets agents reach <url>, which has no authentication: anyone who can reach it can change it, agents included at autonomous and bypass levels. Every command is in the audit log." (one sentence in spec §7; the implementation keeps it as one sentence). One sentence per such source; none when network is off or all sources use credentials.
- The permission level grant shows `bypass` as unavailable (the model forbids it).
- Approving with a credential slot unset is allowed and says so next to the field ("the source is created without a credential; add it later in settings").

**Tests (stop condition):** every section renders from a full fixture handoff; the credential field posts the value once, then clears it, and the value appears nowhere in the DOM afterwards or in the rendered query cache; a failed save shows the error and still clears the field; Save is disabled for a non-pending proposal; the network sentence shows for network on with an `auth: "none"` source (its URL named) and not for network off or credentialed sources; a decided handoff card shows `result`. `npm test`, lint, format check, build; the whole plan: full backend suite, ruff, pyright, `npm run e2e` still green on existing flows (the flow itself is onboarding's); CI green on every job.

---

## Interface gaps

Needed by this plan and absent from (or different in) the overview; each needs a Decision or an overview edit before the task that uses it starts.

- **G1: approval observers.** I19 says `Proposals.decide` is "called by the approvals path on decision" and `create` needs the approval's ID, which exists only after the gate created it. Nothing in the overview says how. Needed: `ApprovalObserver` (`asked`, `decided`) and `ApprovalCanceller` as in task 2, called from the gate and from `record_decisions`, so Stop and expiry are covered. It touches the approvals package, which contracts' task 2 edits too.
- **G2: `ProposalView` is too small.** The tool's result and the card need `result: ApplyResult | None` on the record, `turn_id` internally, a lookup by approval (and by turn and kind for `Propose.run`), and credential slot state (`slots`, with `set`) on a read model `ProposalDetail`. The `proposals` table (contracts task 1) needs columns `approval_id`, `turn_id`, `decided_at`, `result`.
- **G3: a read route.** `GET /api/projects/{id}/proposals/by-approval/{approval_id}` returning `ProposalDetail`: the card has only an approval ID, and the slot route needs the proposal ID.
- **G4: handoff versions table.** I20 says `store` is versioned; contracts' schema must keep every version (`proposal_id`, `approved`, payload), not one row.
- **G5: `ProposalKinds` registry.** Kinds are registered by onboarding but the overview lists no registry; this plan creates `ProposalKinds` (`register`, `get`, `names`) and owns it. The kind names (`connections`, `handoff`) are constants in `proposals/contract.py` (contracts).
- **G6: reusable decision controls.** The note field, Approve and Reject live inline in `ProjectApprovalCard`. The proposal cards must reuse them: contracts' dispatcher refactor must export them (and the status badge) as a component, or tasks 4 and 5 would copy them.
- **G8: ID prefix and events.** `prp` must be in `ids.PREFIXES` and the roadmap, and `proposal.created`, `proposal.decided`, `handoff.approved` in `EventType`: the overview names no owner; contracts task 1 is assumed.
- **G9: the gate and an observer's refusal.** `asked` may raise `DomainError` for a duplicate same-kind proposal in one step; the gate must turn that into a `deny` for that call (tool error) and nothing else. This changes `ApprovalGate.decide` beyond contracts task 2.
- **G10: orphan after a crash.** A crash between `approved` and the sweep leaves a slot credential for a non-pending proposal. The overview has no startup sweep; decision needed: sweep the slots of every non-pending proposal at startup, or accept it.
- **G7: dispatcher keyed on proposal kind.** The overview says the dispatcher switches on "the proposal kind", but an approval part carries only its tool. This plan dispatches on the tool name (`Propose`) in the dispatcher and switches on `proposal.kind` inside `ProposalCard`; contracts' dispatcher needs no proposal knowledge beyond that one line.
