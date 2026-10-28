> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Onboarding Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; no task gets an early PR (nothing consumes this plan). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the stage 3 overview (`docs/superpowers/plans/drafts/2026-10-28-stage-3-overview.md`, on `main` once reviewed) first: it holds I13 to I21 and the shared-file rules.

**Goal:** after one conversation with Onboarding and two approvals, the project is set up and the four specialists start their first audits (spec §9 applying both kinds, §10, §11):
- approving a `connections` proposal creates the connections; approving a `handoff` proposal stores the handoff, creates the tracking sources, sets network and level, starts the sync and starts the kickoff;
- the kickoff posts one system-labeled "Handoff" message to each of Data Quality, Model Performance, Retraining and Deployment & Rollback at once, recorded per agent so a restart neither repeats nor loses it;
- the add-project wizard ends with an Onboarding step;
- all seven definitions describe what each agent can now do, get the roadmap's hard limits and the continuous-PR principle in every system message through `working_rules`, and `status_board:` is gone from the definition format; Onboarding's prompt covers every field of the full `HandoffPayload` (I19); agents read the stored handoff with the new `ReadHandoff` tool;
- an end-to-end test (fake models, a fixture repository and data folder) proves the whole flow.

**Branch:** `plan/stage-3-onboarding`.

**Consumes:** every other stage 3 plan merged on `main`: I13 (stall and `propose`), I14, I15 `Connections`, I16 `Tracking`, I17, I18 `HookRegistry` and the hooks `status_board`, `data_summary`, `tracking_summary`, `knowledge`, I19 `Proposals` and `ProposalKind`, `CredentialSlot`, `ApplyResult`, the payload models `ConnectionsPayload` and `HandoffPayload`, `ProjectSecrets` (`credentials/project_secrets.py`: `move`, `delete_prefix`), `ProposalKinds`, I20 `Handoffs`; the tools `SaveNote`, `SearchNotes`, `TrackingHistory`, `Propose`; the payload validation in contracts (IANA zones, on-call hours, command fields, declared placeholders, one `Cadence` field, references between parts resolve); the fake MLflow server (tracking); the `kickoff` table (contracts' stage 3 migration); stage 2's `AgentSessions.post`, `Sandbox.set_network`, `set_permission_level`, `EventLog.record`.

**Produces:** the `ReadHandoff` tool (risk class `read`), the `connections` and `handoff` proposal kinds (`onboarding/`), `Kickoff` (`onboarding/kickoff.py`), the seven definitions, the Onboarding wizard step (`/projects/:projectId/onboarding`), the scripted fake models, and the end-to-end tests (backend and Playwright).

**Read first:** spec §9 (Applying the handoff, The kickoff), §10, §11, §15 (Kickoff and End to end); `agents/messaging/wakeups.py` (`fire_due`: how a labeled post is made and guarded against a restart); `agents/runtime/sessions.py` (`post`, `_enqueue`); `agents/runtime/executor.py` (`AgentBuilder.build` runs again when a waiting turn resumes, so hooks are re-rendered after an approval).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | `ReadHandoff` and the seven definitions, without `status_board` | data-summary (hook registry, `status_board` hook), knowledge, tracking and proposals tool names, I20 `Handoffs` |
| 2 | The kickoff | contracts (kickoff table), I20 |
| 3 | The `connections` and `handoff` kinds | task 2 (`Kickoff`, `handoff_preview`), I15, I16, I19, I20 |
| 4 | The Onboarding wizard step | task 1 (nothing else: the step posts a chat message) |
| 5 | End to end | tasks 1 to 4, the fake MLflow server |

No task has an early PR; the plan's PR is opened after task 5.

---

### Task 1: `ReadHandoff` and the seven definitions, without `status_board`

**Files:**
- New `backend/src/mlops_agents/agents/tools/read_handoff.py` (`ReadHandoffTool`); `agents/tools/__init__.py` (`default_registry` gains `handoffs: Handoffs | None = None` and one registration line, your own lines only); `api/services.py` (pass `handoffs`); tests `backend/tests/agents/tools/test_read_handoff.py`.
- `backend/src/mlops_agents/agents/definitions/{orchestrator,onboarding,data-quality,model-performance,retraining,deployment,alert-prioritization}.md` (all seven).
- `backend/src/mlops_agents/agents/catalog.py`: remove `AgentDefinition.status_board` and the transitional treatment of `status_board: true` as the `status_board` hook; the overview (I18 and "Files shared") gives this one removal to this plan; data-summary owns every other edit of `catalog.py` and has merged first.
- New `backend/src/mlops_agents/agents/prompts/hard_limits.md` and `team_system.md` (packaged text, outside `definitions/` because the catalog loads every `.md` there), `backend/src/mlops_agents/agents/working_rules.py` (`working_rules(definition: AgentDefinition) -> str`), and one added line in `agents/runtime/context.py` (`build_messages`: `sections.append(working_rules(definition))` right after the definition's prompt). The overview gives this line to this plan; data-summary changed `build_messages` before it.
- Tests: `backend/tests/agents/test_agent_definitions.py`, `backend/tests/agents/test_catalog.py`, and every test that hard-codes the tool set (`STAGE_2_TOOLS` in `backend/tests/conftest.py` and in `test_agent_definitions.py`): grep for `STAGE_2_TOOLS` and `status_board` under `backend/tests` and update each user.

**Interfaces:** Consumes I18 (`HookRegistry.names()`, the catalog validating `context_hooks` against it), I20 `Handoffs.current` and the four tool names. Produces `ReadHandoff` and the definitions.

**`ReadHandoff`:**
- `name = "ReadHandoff"`, risk class `read` (justified: the handoff is in the app's database); args `section: Literal["models", "commands", "data_roles", "tracking", "pipeline", "edges", "operations", "gaps", "grants"] | None = None`, the section names being the `HandoffPayload` field names (a test pins the Literal to `HandoffPayload.model_fields`, so a new part must be added).
- No section: the list of sections, each with its item count and size in characters, so the agent picks one. A section: that part of the payload as JSON (`model_dump_json`, indented). No handoff yet: a plain message that Onboarding has not handed off, not an error.
- Capped per section by one constant (`READ_HANDOFF_SECTION_CAP`, characters): a list section drops whole trailing items past the cap and ends with "N more items omitted" (when the first item alone exceeds the cap, it is cut at the cap with a "cut at the cap" note and the rest omitted, never an empty result); `operations` (an object) is read by its sub-keys, `ReadHandoff("operations")` returning each sub-key's own capped JSON in turn, with the same omission note. The payload holds no secret, so nothing is scrubbed.
- Everyone who has the tool reads the same current handoff; it never writes.

**Implementation notes:**
- Frontmatter per the §11 table plus `ReadHandoff` (overview, I20), exactly:

| Agent | Added tools | `context_hooks` |
|---|---|---|
| orchestrator | SaveNote, SearchNotes, TrackingHistory, ReadHandoff | status_board, tracking_summary, knowledge |
| onboarding | SaveNote, SearchNotes, TrackingHistory, ReadHandoff, Propose | data_summary, knowledge |
| data-quality | SaveNote, SearchNotes, ReadHandoff | data_summary, knowledge |
| model-performance | SaveNote, SearchNotes, TrackingHistory, ReadHandoff | data_summary, tracking_summary, knowledge |
| retraining | SaveNote, SearchNotes, TrackingHistory, ReadHandoff | data_summary, tracking_summary, knowledge |
| deployment | SaveNote, SearchNotes, TrackingHistory, ReadHandoff | tracking_summary, knowledge |
| alert-prioritization | none | none |

- Remove the `status_board:` line from all seven files. Only agents that had `status_board: true` get the hook: read all seven files for it (today the Orchestrator's; do not assume). Onboarding's `max_steps` becomes 60. Update the Onboarding description: it no longer "proposes tests and monitoring plans".
- `AgentDefinition` has `extra="forbid"`, so a leftover `status_board:` line fails the load; the all-definitions test below catches it.
- **Shared rules live once, not in the seven files.** `hard_limits.md` holds the roadmap's "Hard limits" as one bullet per roadmap bullet, in order, lightly edited so each reads alone (the roadmap's "merging only as above" points at a table the agent never sees: the file says instead "no merging; where a later stage allows it, only when the platform reports the PR clean, with at least one passing check, never with an admin override, and never for a PR that touches the team's protection surfaces"). `team_system.md` holds the continuous-PR principle ("Agents and the team's system"), keeping every rule: search open PRs, the team's included, and update a matching agent PR instead of duplicating; one concern per PR, owned by the agent whose area it is; a cap on open improvement PRs, fixes for open incidents exempt; PRs that change decision semantics carry the `decision-semantics` label and state the behavior before and after; stale or conflicting agent PRs are updated or closed with a note; a PR closed unmerged becomes a Knowledge note with its reason; after an incident, link recent changes (the team's and the agents') to it, and revert only your own. `working_rules(definition)` returns the hard limits, then, when the definition's tools include `OpenPullRequest`, the team-system text, otherwise the one line "Changes to the team's system go through PRs by the agent whose area it is; you route, you do not open them." `build_messages` appends it to every agent's system message, so the seven definition files carry neither text.
- Prompts describe what each agent can do now, in the existing style (a "What you can do now" list and "How to work"), and nothing it cannot yet do:
  - connections are read-only at `/data/<connection name>` in the sandbox, with `duckdb` and pandas for exploring; never load a whole table into the context (the `data_summary` hook is the overview);
  - `ReadHandoff` where the agent has it: the approved handoff, read by section, is the source of what each model, feed, command and check is; read it instead of asking the user again;
  - the hooks the agent has, and what each gives it; notes (`SaveNote` for what it learns, `SearchNotes` before asking again); `TrackingHistory` where the agent has it;
  - Onboarding: the flow of spec §10 (read the repository and ask; propose `connections`, wait for approval, read only the repository until then; explore the mounted data; propose the `handoff`; missing predictions and labels, and no tracking, each become a PR and a Gap), and that a newer approved proposal replaces the current one. The `handoff` section names every part of the payload and what to find for it, in the payload's own words, without field-level syntax (the tool's schema carries that):
    - per model (`models`): the profile (objective in the user's words, problem type, target column, positive class, primary metric and why, operating threshold, label delay and label maturity in days, serving mode batch, online or streaming, what each error costs and the cost ratio, the groups to slice by); the model's predictions and labels tables, each a reference to a table already given a `predictions` or `labels` data role; its commands (train, evaluate, deploy, rollback, rerun, hold, fall back), each either a command template or a CI workflow dispatch, with the inputs and the placeholders it takes, whether it deploys, and the entry files it runs; whether promotion is by the team's gate or by a person; its environments (tracker alias to environment); and its production signal (tracker alias, CI job or file);
    - project level: commands that belong to no model (rerun a data job, hold or quarantine a batch); the data roles per table (`training`, `serving_inputs` as the model's raw inputs as served, `predictions`, `labels`, `other`) with the join ID column and time column; the tracking sources; the pipeline stages and the edges between them;
    - operations: the scheduler and where it is configured; feeds (data or job, where, cadence as a cron or an interval, grace in minutes, time zone); who is on call (contact, Discord or email, structured weekday hours, time zone, escalation order); the team's existing checks (stable id, category, cadence and time zone, where results land: a file pattern, a CI workflow or an MLflow metric); where results are kept overall;
    - gaps, each with its next step, and the grants (network and a permission level, never `bypass`);
    - what it could not find it asks the user about once, then records as a gap; it never invents a command, a feed or a contact. Stored commands, feeds, on-call details and checks are read by later stages: say so, do not promise they run now;
  - the four audit agents: a message authored "Handoff" starts the first audit of their part; the audit ends with a report (what is good, broken or missing, with evidence), saves notes, and uses the stage 2 tools and gates for PRs and issues; replies stay in their own session;
  - checks and result reading are stage 4 and deploy hooks stage 5: where a prompt mentions them, it says they arrive later. Alert Prioritization only changes its stage wording (it still does not run; no tools or hooks added).
- Nothing in `agent.py` or `sessions.py` changes in this task, and `services.py` only gains the `handoffs` line; `context.py` changes by the one line above.
- Prove nothing still reads the removed field: `grep -rn "\.status_board\|status_board:" backend/src` shows only `StatusBoard` (the events service) and the hook's own name. If `agents/runtime/agent.py` still reads `definition.status_board`, data-summary has not finished its change: stop and say so in the plan issue.

**Tests (stop condition):**
- `ReadHandoff`: with no handoff, the plain message; with a full handoff, no section lists every section with counts; each section returns its part (a model's name and promotion appear in `models`); a section over the cap drops whole trailing items and says how many; one item alone over the cap is cut at the cap with a note, never an empty result; `operations` returns its sub-keys; the section Literal equals `HandoffPayload.model_fields`; the tool is risk class `read`, and it writes nothing.
- With the real `default_registry`, the real `HookRegistry` and `AgentCatalog.load`, all seven definitions load (no unknown tool, no unknown hook, no leftover field).
- A table-driven test compares every agent's `tools`, `context_hooks` and `limits.max_steps` with the §11 table above, and asserts only the Orchestrator has `status_board` in its hooks.
- `parse_definition` of a file with a `status_board:` line raises `DefinitionError`.
- Prompt checks that guard the "nothing it cannot do yet" rule: no prompt of the seven names a tool the agent lacks; Onboarding is the only prompt that mentions `Propose`; `ReadHandoff` is named by the six agents that have it and not by Alert Prioritization; the four audit agents' prompts mention "Handoff".
- Shared rules: the test parses the "Hard limits" bullets out of `docs/roadmap.md` and asserts the packaged `hard_limits.md` has the same number of bullets, each beginning with the roadmap bullet's first 25 characters (so the file may be edited for reading, not dropped or reordered); `working_rules` of an agent with `OpenPullRequest` contains the continuous-PR rules (search open PRs, one concern per PR, the cap, `decision-semantics`, the Knowledge note on a closed-unmerged PR, link recent changes after an incident) and of Alert Prioritization the routing line and no PR rules; `build_messages` puts the rules in the system message of all seven agents, once; none of the seven definition files contains the hard-limits text.
- Onboarding coverage: a table-driven test lists every field name group of the payload (`models`, `profile`, `predictions`, `labels`, `promotion`, `environments`, `production signal`, `placeholders`, `entry files`, project-level `commands`, `data roles`, `edges`, `feeds`, `grace`, `time zone`, `on call`, `escalation`, `checks`, `results location`, `gaps`, `grants`) and asserts the Onboarding prompt names each (case-insensitive), so adding a payload part without the prompt fails; a second test builds the list from `HandoffPayload.model_fields` and `ModelSpec`/`Operations` field names, so a new contracts field fails until the prompt covers it (the test maps a field to its words in one dict next to the test).
- Full backend suite; `uv run ruff check`, `uv run ruff format`, `uv run pyright`.

---

### Task 2: The kickoff

**Files:** new `backend/src/mlops_agents/onboarding/__init__.py`, `onboarding/briefs.py`, `onboarding/kickoff.py`, `onboarding/handoff_preview.py` (the one renderer of a `HandoffPayload`, shared by the kickoff and by task 3's kind: `sections(payload) -> dict[str, str]`, one function per section, in the fixed order models, project commands, data roles, tracking, pipeline, operations, gaps, grants; `render(payload) -> str` joins them), tests `backend/tests/onboarding/test_handoff_preview.py`; `backend/src/mlops_agents/api/services.py` (your own lines only: build `Kickoff`, call `resume()` once after the sessions exist, add it to `Services`); tests `backend/tests/onboarding/test_kickoff.py`.

**Interfaces:**
- Consumes `AgentSessions.post`, `Handoffs.current` (I20: `HandoffView(proposal_id, approved, payload)`), `Databases`, `Handoffs.store` (I20: idempotent by proposal, returns the version `int`) and the `kickoff` table (contracts: `handoff_version`, `agent`, `message_id` null until posted, `status` `pending` or `posted`, `posted_at`; key `(handoff_version, agent)`), the `messages` table. `HandoffView` carries no version: `Kickoff.start` receives the version `store` returned, and `resume` gets the current handoff's version by calling `Handoffs.store(project_id, view.proposal_id, view.payload)` again, which is idempotent and returns the same version.
- Produces:

```python
KICKOFF_AGENTS: Final[tuple[str, ...]] = ("data-quality", "model-performance", "retraining", "deployment")
AUDIT_BRIEFS: Final[dict[str, str]]    # briefs.py: agent name -> that agent's audit brief; keys == set(KICKOFF_AGENTS)
HANDOFF_AUTHOR: Final = "Handoff"      # the message's author_name, as the wake-ups use "Scheduled wake-up"

class Kickoff:
    def __init__(self, databases: Databases, sessions: AgentSessions, handoffs: Handoffs,
                 ) -> None: ...
    async def start(self, project_id: str, version: int, proposal_id: str, payload: HandoffPayload) -> list[str]: ...  # agents posted now
    async def resume(self) -> None: ...   # startup: finish any kickoff of a project's current handoff
```

**Implementation notes:**
- Posting is exactly stage 2's wake-up post: `sessions.post(project_id, agent, author_name=HANDOFF_AUTHOR, from_agent=agent, text=text, exchange_id=None, hop=None)`. The recipient is its own `from_agent`, there is no exchange, so no hop or token limit applies, nothing wakes Onboarding, and replies stay in the agent's own session. `author_kind` stays what `_enqueue` sets today (`"user"`): the label is the author name, as with wake-ups. Do not touch `sessions.py`.
- The text is `[handoff <proposal_id>]` (the marker, first line), then the summary, then that agent's `AUDIT_BRIEFS` entry. The summary is built from `handoff_preview.sections(payload)` (the renderer the kind's `preview` also uses, so there is one): it holds no secret because a payload never does. A fixed cap constant in `briefs.py` applies to the whole message: each agent's own sections (see its brief below) are rendered first and the rest after them, and the text is cut at a section boundary under the cap, ending with the line "(cut: read the rest with ReadHandoff)". The cut is logged.
- The four are posted with `asyncio.gather`, so the audits start at once. A busy agent session queues the message (stage 2's busy rule); that is not a failure.
- **Exactly once, nothing lost.** Per (project, version) one `asyncio.Lock` serializes `start` and `resume`. First `start` writes the four rows with status `pending` (`INSERT ... ON CONFLICT DO NOTHING`), so the intent survives a crash. Then for each agent whose row is `pending`:
  1. look for the marker in that agent's earlier posts (the guard `wakeups.fire_due` uses, for a crash between post and mark): `select messages.id where from_agent == agent and author_name == "Handoff" and parts LIKE '%[handoff <proposal_id>]%'`. Found -> mark the row `posted` with that message ID, do not post;
  2. else post, then mark the row `posted` with the new message ID.
  A row already `posted` is skipped. A failed post is logged with its traceback (`logger.exception`) and the row stays `pending`, so the next `resume` retries it; the other three still go out.
- `resume()` at startup, before the app serves: for every project (list them the way `WakeupService._get_all_project_ids` does; a project whose database cannot be opened is logged and skipped), read `Handoffs.current`; when one exists, run the loop above (inserting any missing row as `pending` first). This closes both windows: a crash after the handoff was stored and before any row was written, and one between a post and its mark. Rebuild the summary from the stored payload (`HandoffView.payload`), so a restart needs nothing the process lost.
- Each new approved handoff (a later proposal ID) starts its own kickoff, as §9 says applying the handoff starts it; the brief tells the agent to re-audit what the new handoff changes. State this in the PR description.
- Each audit brief names that agent's part of the handoff to study first: Data Quality the data roles, feeds and `serving_inputs`; Model Performance each model's profile, predictions and labels references and label delay; Retraining each model's train and evaluate commands, entry files and promotion mode; Deployment & Rollback each model's deploy and rollback commands, environments, production signal and promotion mode. Every brief tells the agent to use `ReadHandoff` for any part it needs in full. Briefs are plain text constants; a test checks each names the sections above, and that each brief's section keys exist in `handoff_preview.sections` (so the cut keeps an agent's own sections first).
- An agent with no usable model: the post succeeds, the turn is refused with the stage 1 message in that agent's session, and the row stays. The user fixes the model and asks again; the kickoff never silently repeats.

**Tests (stop condition)** (a real `SessionManager` or the stage 2 fake sessions in `tests/api/fake_sessions.py`, a real project database):
- `handoff_preview`: every section renders for a full payload (assertions in task 3's preview test); `render` is the sections joined in the fixed order.
- A summary past the cap is cut at a section boundary with the agent's own sections kept, and ends with the cut line; an uncut summary has no cut line.
- `start` posts four messages, one per kickoff agent, each `author_name == "Handoff"`, `from_agent == agent`, `exchange_id` and `hop` null, containing the marker, the summary and that agent's brief; four rows `posted`; the call returns the four names.
- A second `start` for the same proposal posts nothing.
- Crash windows: post succeeded but the row still `pending` → `resume` marks it `posted` and posts nothing; handoff stored but no rows and no messages → `resume` writes them and posts four; one agent's post raising → the other three are posted and recorded, and the next `resume` posts only the pending one.
- A restart (a new `Kickoff` and `SessionManager` over the same data folder) neither repeats nor loses a kickoff.
- A newer approved handoff (a new version) posts four more messages with its own marker; the same proposal applied twice posts none twice.
- Onboarding's session gets no message from the kickoff, and replies to the four do not create an exchange or wake Onboarding.
- `set(AUDIT_BRIEFS) == set(KICKOFF_AGENTS)`, and every name exists in `AgentCatalog.load(...)` with `chat: true`.
- Full suite and checks.

---

### Task 3: The `connections` and `handoff` kinds

**Files:** new `onboarding/connections_kind.py`, `onboarding/handoff_kind.py`, `onboarding/handoff_parts.py` (one function per part of the apply, kept out of the kind so each stays small), `onboarding/handoff_check.py` (the cross-reference check); `api/services.py` (your own lines: construct the two kinds with their services and register them); tests `backend/tests/onboarding/test_connections_kind.py`, `test_handoff_kind.py`.

**Interfaces:**
- Consumes `ProposalKind` (with `warnings`), `CredentialSlot`, `ApplyResult`, `ConnectionsPayload`, `HandoffPayload` (contracts' `proposals/payloads.py`: `ConnectionsPayload.connections: list[ConnectionSpec(kind, name, settings, why)]`; `HandoffPayload(models: list[ModelSpec], commands, data_roles, tracking, pipeline, edges, operations: Operations(scheduler, feeds, on_call, checks, results_location), gaps, grants: Grants(network, permission_level))` with `ModelSpec(name, profile, predictions: TableUse | None, labels: TableUse | None, commands, promotion, environments, production_signal)`, exactly as I19 writes them; `permission_level` is a `GrantableLevel`, never `bypass`), `Connections.list/create/tables` (I15), `Tracking.sources/create_source/sync` (I16), `Handoffs.store` (I20), the `ProjectSecrets` instance (`move`, `delete_prefix`; contracts, built once in `build_services` and passed to the handoff kind's constructor as `secrets`) and the module ref builders `proposal_slot_ref`, `proposal_prefix` and `tracking_ref` (the only code that builds a ref string; this plan writes none), `ProposalKinds.register` (proposals), `Sandbox.set_network`, `set_permission_level(databases, project_id, level)`, `EventLog.record`, `Kickoff.start`, `handoff_preview.render` (task 2).
- Produces two `ProposalKind` classes, `ConnectionsKind` (`name = CONNECTIONS_KIND`) and `HandoffKind` (`name = HANDOFF_KIND`), the constants of `proposals/contract.py` (contracts; no kind-name literal in this plan's code), each with `payload_model`, `preview`, `warnings`, `credentials_needed` and `apply`; `apply` is idempotent as I19 requires.

**Implementation notes:**
- **`ConnectionsKind`.**
  - `preview`: `Preview(kind="text", title="Connections", body=...)`: per entry, name, kind, settings and its `why`; for a `local_folder`, the path. `preview` is pure, sync and cheap (the `Propose` tool calls it twice per call), so it holds no findings: the proposals card shows the denylist warning by calling `POST .../connections/inspect` (proposals' task).
  - `credentials_needed`: none (no connection kind in this stage needs one).
  - `apply`: one `Connections.create(project_id, kind, name, settings)` per entry, in order, each its own part named `connection:<name>`. Validation errors (a folder inside the app's data folder, a missing path) are `DomainError`s with user-facing messages: reported under `failed`, the rest applied. Idempotent: a connection of that name with the same kind and settings already existing counts as applied; the same name with other settings fails with "a connection named X already exists with other settings".
- **`HandoffKind`.**
  - `preview`: one readable `Preview(kind="text", ...)`, a section per part in this order: each model (name, the profile in plain lines, predictions and labels references, commands with type, template or workflow, placeholders, `deploys` and entry files, promotion mode, environments, production signal), project-level commands, data roles, tracking sources, pipeline stages and edges, operations (scheduler; feeds with cadence, grace and time zone; on call with hours, time zone and order; checks with category, cadence, time zone and where results land; the results location), gaps, grants. `Cadence` renders as the cron or "every N minutes". A missing optional value renders as "not set", never as nothing. The body is `handoff_preview.render(payload)` (task 2), the renderer the kickoff also uses; it is pure and cheap (the `Propose` tool calls `preview` twice per call). The preview carries no warning text of its own: the network sentence comes from `warnings` below.
  - `warnings(payload) -> list[str]` (I19, user-facing text the proposals card shows as given): when an `mlflow` source has `auth == "none"` and `grants.network` is on, the §7 sentence (an unauthenticated tracker can be changed by agents at `autonomous` and `bypass`, and every Bash command is audited); a `github_actions` source is always `auth: "none"` and uses the app's GitHub login, so it never triggers it. The `connections` kind's `warnings` returns an empty list (its findings come from the card's `inspect` call). The sentence is defined once, here; the frontend copies none of it.
  - `credentials_needed`: one `CredentialSlot(index=i, label=..., source_type=...)` per tracking source with `auth == "credential"`, `i` being the source's position in the payload.
  - `apply` runs these parts in order, each wrapped by one helper that records `applied.append(part)` or `failed[part] = reason` and never lets one part stop the next. The helper catches `DomainError` (its message is the reason) and, at this third-party-free boundary only, `Exception` for the unexpected: it logs the traceback and records the generic reason "unexpected error, see the log"; reasons pass through `scrub_values` with the project's secrets before they are stored. No bare `except`.

| Part | Does | Depends on |
|---|---|---|
| `handoff` | `handoff_check.check(payload, connections, tables)` (below), then `version = Handoffs.store(project_id, proposal_id, payload)` (an `int`) with the whole payload | none; a failed check fails the part with every problem listed ("needs the connection X first", "the connection X has no table T") and nothing is stored |
| `tracking:<i>` | `Tracking.create_source(project_id, type, settings, auth)`; for `auth == "credential"` then `secrets.move(proposal_slot_ref(project_id, proposal_id, i), tracking_ref(project_id, source.id))`; a slot never filled leaves the source created with `credential_set` false (the Runs page shows the sync error) | none; one source failing does not stop the others |
| `network` | `Sandbox.set_network(project_id, grants.network)` | none |
| `level` | `set_permission_level(databases, project_id, grants.permission_level)`; `bypass` is refused here too (`failed["level"] = "a proposal cannot set bypass"`), even though the payload model forbids it, and the level is left unchanged | none |
| `sync` | `Tracking.sync(project_id)`, which only starts a sync (I16); `tracking.synced` and the source's `last_error` report its end, not this result | at least one `tracking:*` applied |
| `event` | `EventLog.record(project_id, source="onboarding", type="handoff.approved", subject_id=proposal_id, payload={"proposal_id": proposal_id})` (no payload field beyond the ID) | `handoff` applied |
| `kickoff` | `Kickoff.start(project_id, version, proposal_id, payload)`, `version` being what the `handoff` part's `Handoffs.store` returned | `handoff` applied; otherwise `failed["kickoff"] = "not started: the handoff was not stored"` |

  - **The cross-reference check** (`handoff_check.py`; only what needs project state; the payload's own validation is contracts' and is not repeated): every `DataRole` and `FileResults.connection` (checks and `results_location`), names an existing connection, and a `DataRole` an existing table of it (a `TableUse` is a `DataRole` by contracts' validation). References between parts of the payload itself (`TableUse` to a role, alias maps, edges) are contracts' validation, as are time zones, hours, command fields, placeholders and `Cadence`. Each failure is one line naming the part.
  - `apply` is idempotent: running it again for the same proposal gets the same version back from `Handoffs.store` (idempotent by proposal), skips a tracking source whose settings match one of the project's existing sources, and `Kickoff.start` posts nothing twice.
  - Credentials: after the `tracking:*` parts, in a `finally`, `secrets.delete_prefix(proposal_prefix(project_id, proposal_id))` removes every slot secret still there (a source that failed to create), as §9 requires; reject, cancel and expiry are the proposals observer's. Secrets already moved are untouched.
  - No secret appears in a preview, an `ApplyResult`, an event or a log line: slot references are built from IDs only, and the credential value is never read in this plan.
- Registration: `build_services` registers both kinds with `ProposalKinds.register` (proposals builds the registry first); the kinds hold injected services, never look them up. Re-applying an approved proposal with no `result` after a restart is proposals' (I19); it is why `apply` is idempotent.

**Tests (stop condition)** (real `Databases`, real or fake `Connections`/`Tracking` as the producing plans' own fakes allow, the fake MLflow server for a real source):
- Connections: approving two entries creates both; a folder inside the data folder fails and the other is created; the same call twice creates one connection; same name with other settings fails and leaves the existing connection.
- Full payload: a handoff with two models (one with `team_gate` promotion and environments, one with `person` and a `file` production signal), project-level `hold` command, feeds with a cron and with an interval, an on-call entry with two weekday ranges and a time zone, edges, checks of the three result kinds and a results location is stored whole: `Handoffs.current` returns a payload equal to the one approved.
- Cross-reference check, one test per rule: a `DataRole` and a `FileResults.connection` naming a connection that does not exist, and one naming a missing table: each fails the `handoff` part with its reason, and nothing is stored.
- `preview` shows every section (a test asserts each model's name, its promotion mode, an environment, the production signal, a placeholder, a feed's cron and time zone, an interval feed as "every N minutes", the on-call hours, an edge, a check's results location, the results location, a gap and the grants), a missing optional renders "not set", and the kind's `preview` body equals `handoff_preview.render` for the same payload.
- Handoff: all parts applied → `applied` lists them all, the handoff is current, the sources exist, network and level are set, `handoff.approved` is recorded, and the kickoff posted four messages.
- A tracking source whose creation raises: `failed["tracking:1"]` has the reason, the other parts are applied, the kickoff still starts.
- A role naming a missing connection: `failed["handoff"]`, nothing stored, the kickoff and the event not run, network, level and the sources still applied.
- `bypass` in the payload (bypassing the model through a hand-built object): the level part fails and the level stays.
- A credential: with a slot filled before apply, the secret is readable at `tracking_ref(...)` and gone from the slot reference afterwards; a slot of a source that failed to create is deleted by `apply`; the approval preview, `ApplyResult`, the events and the audit rows contain neither the value nor anything of it (assert on the serialized text).
- Running `apply` twice: one current handoff version per proposal, one source, four kickoff messages in total.
- `warnings` of a handoff with an unauthenticated `mlflow` source and network on returns the network sentence; with network off, with a credential, or with only a `github_actions` source, it returns none; the `connections` kind returns none.
- Full suite and checks.

---

### Task 4: The Onboarding wizard step

**Files:**
- New `frontend/src/pages/onboarding/OnboardingStepPage.tsx`, `onboarding/route.tsx` (path `/projects/:projectId/onboarding`, no `nav`: the sidebar lists only pages that work), `onboarding/paths.ts` (`onboardingPath(projectId)`, in a `paths.ts` as `PROJECTS_PATH` is), tests `onboarding/OnboardingStepPage.test.tsx`.
- `frontend/src/pages/add-project/LocalFolderTab.tsx` and `GitHubTab.tsx`: `onSuccess` navigates to `onboardingPath(project.id)` instead of `/projects/${project.id}`.
- Tests that follow the new destination: `add-project/AddProjectPage.test.tsx`, `add-project/AddProjectErrors.test.tsx`, `add-project/GitHubTab.test.tsx` (their test routes and expectations name `/projects/:projectId`).

**Interfaces:** Consumes `useProjectAgents` (the Onboarding entry's `settings.effective_model` and `settings.model_problem`: the one usable-model rule lives in the backend, do not re-derive it), `GET /api/projects/{project_id}/sandbox` (`docker.available`, `docker.detail`), `useProject`, `useSession`, `POST /api/projects/{project_id}/agents/{agent}/session/messages` (`{text}`). All types come from the generated `schema.d.ts`; no new API.
Produces the wizard step.

**Implementation notes:**
- The step shows three rows:
  1. **Model** (required): ready when the Onboarding agent has an `effective_model` and no `model_problem`; otherwise the problem text (or "No chat model is set for Onboarding & Diagnosis") and a link to AI providers; Start stays disabled.
  2. **Docker**: the status text from `/sandbox` (`docker.detail` when unavailable), with the sentence "Without Docker, Onboarding can read the repository but not run commands." Start stays enabled either way.
  3. **Start**: posts the first message to Onboarding's session, from the user, naming the project and its repository (`github_full_name`, else `local_path`), from one constant template, then navigates to `/projects/{id}/agents?agent=onboarding` (the param `AgentsPage` reads). A failed post shows the error text in an alert and stays on the step.
- If Onboarding's session already has messages (the step is reached again), Start becomes "Open Onboarding" and posts nothing.
- A project lookup failure or an agents lookup failure shows its error text, never a ready state.

**Tests (stop condition)** (Vitest and Testing Library, `fetch` stubbed as the neighbouring tests do):
- No model: Start disabled, link to AI providers shown; a model problem shows its text.
- Docker unavailable: the sentence and the detail are shown; Start enabled; Docker available: no sentence.
- Start posts exactly `{text}` containing the project's name and repository to `.../agents/onboarding/session/messages` once (double click posts once), then lands on the agents page with `agent=onboarding`.
- A 503 on the post: alert with the message, no navigation, Start usable again.
- A session with messages: "Open Onboarding", no post.
- Both add-project tabs navigate to `/projects/<id>/onboarding` after adding.
- `npm run lint`, `npm run format:check`, `npm test`, `npm run build`; `npm run e2e` is task 5's, but the existing stage 1 steps of `flow.spec.ts` that follow "add the repository" must keep passing: update that step's expectation from the agents URL to the onboarding step, then open Agents through the sidebar link (the stage 1 test sets a model for the Orchestrator only, so Start stays disabled there). State the exact lines changed in the PR description.

---

### Task 5: End to end

**Files:**
- `backend/tests/fakes/onboarding_script.py` (new: `onboarding_reply(messages, tools)` and `audit_reply(messages)`), and in `backend/tests/fakes/fake_openai.py` two entries in `MODELS` (`fake-onboarding`, `fake-audit`) and two dispatch lines in `_reply`, placed right after the `report_capabilities` branch where `fake-write` sits (the generic "last message is a tool result" branch below ends any script with "Done. First line: ...", so a later position would stop `fake-onboarding` after its first `Propose`): the overview lets onboarding add its scripted models there, in its own functions; nothing else in that file changes, and no test counts `MODELS` entries (the other plans add their own).
- `backend/tests/fakes/test_onboarding_script.py` (the scripted payloads validate against the real `ConnectionsPayload` and `HandoffPayload`, so a contracts change fails here, not in the browser; the handoff payload also passes `handoff_check.check` against the fixture folder's tables).
- `backend/tests/onboarding/test_onboarding_flow.py` (in-process end to end, including a restart).
- `frontend/e2e/flow.spec.ts` (a second test; the stage 1 test is untouched except task 4's line), `frontend/e2e/repo.ts` (`createDataFolder()`), `frontend/e2e/env.ts`, `frontend/e2e/run.mjs`, `frontend/playwright.config.ts` (the fake MLflow server as a third `webServer`).

**Interfaces:** Consumes everything above and the tracking plan's fake MLflow server. Produces the end-to-end tests and the two scripted fake models.

**Implementation notes:**
- **Scripted models.** No Bash and no Docker anywhere in the scripts (e2e CI has no Docker). The fake derives its step from the conversation, never from server state, so tests stay independent:
  - `fake-onboarding` (must answer the verify step like any fake model: that branch comes first in `_reply`):
    - no user message contains `DATA_PATH=` → plain text asking where the data lives (this answers the wizard's first message);
    - `DATA_PATH=<path>` present and no tool result yet → `Propose(kind=CONNECTIONS_KIND, payload=...)` with one `local_folder` connection named `hotel-data` at that path (the value runs to the end of its line);
    - one tool result → `Propose(kind=HANDOFF_KIND, payload=...)` with a payload covering each part of the full `HandoffPayload`: one model `hotel-cancel` (a profile with metric `f1`, `predictions` and `labels` referencing the `predictions_e2e` and `labels_e2e` tables, a `train` command of type `command` with a template and one placeholder, `team_gate` promotion, one environment, a `tracker_alias` production signal); one project-level `hold` command; roles `training` (`bookings_e2e`), `predictions` (`predictions_e2e`, with ID and time columns) and `labels` (`labels_e2e`); one MLflow tracking source at the URL given as `MLFLOW_URL=<url>` in the same user message with `auth: "none"`; two pipeline stages and one edge; operations with a scheduler, one cron feed with a time zone, one on-call entry with structured hours, one check with `FileResults` and a results location; one gap; grants `network: false` and `permission_level: "ask"`. The payload is built by one function in `onboarding_script.py`;
    - two tool results → a final text ("Onboarding is complete.").
    Payloads are built as Python dicts and serialized with `json.dumps` (a Windows path's backslashes must survive).
  - `fake-audit`: no tools; it searches every message's content (a string or a list of parts) for the first line containing `bookings_e2e` and answers `Audit context included: <that line, cut at 160 characters>`; with no such line it answers `Audit saw no data.` The assertion on this text proves the `data_summary` hook reached the turn's context.
- **Fixtures.** The data folder holds `bookings_e2e.csv`, `predictions_e2e.csv` and `labels_e2e.csv` (each a header and 7 rows, the last two sharing an ID column) and one unsupported file (`notes.txt`). It lives in the OS temp folder under its own prefix, never inside `E2E_DATA_DIR` (connections refuse paths inside the app's data folder). On Windows DuckDB or the sync may still hold files when a spec ends, so (as Decision #143 did for the data folder) `run.mjs` makes the folder once, passes it as `E2E_FIXTURE_DIR`, and removes it after Playwright exits, with the same guard: only a folder named with the prefix, directly in the OS temp folder, real paths compared. Factor the guard so both removals use one function (no copy). The project repository is a second `createTestRepo()`.
- **Fake MLflow.** The tracking plan's fake server (`python -m tests.fakes.fake_mlflow --port <n>`, I16) is started as a third Playwright `webServer` on a new port constant in `e2e/env.ts` (`MLFLOW_PORT`, `FAKE_MLFLOW_URL`) next to the two existing ones; the first sync succeeds with the server's seeded experiment. The backend test serves the same fake's ASGI app in process the way `start_fake_server` does for the OpenAI fake.
- **Playwright flow** (a second `test`, its own repository, its own random provider name; `workers: 1` is already set):
  1. sign in; add the fake as a self-hosted provider; add models `fake-onboarding` and `fake-audit` and verify both; on the AI providers page set `fake-onboarding` as Onboarding's model and `fake-audit` as the model of Data Quality, Model Performance, Retraining and Deployment & Rollback (the existing "Model for <agent>" comboboxes);
  2. add the repository as a local project: the Onboarding step opens; its model row is ready; Start; the Onboarding session opens with the wizard's message and the fake's question;
  3. send `DATA_PATH=<path>` and `MLFLOW_URL=<url>` in one message; a `connections` approval card shows `hotel-data` and the folder path; Approve; the turn continues (it is a resumed turn, so the `data_summary` hook is re-rendered); a `handoff` approval card shows the model `hotel-cancel` with its promotion mode, the three roles, the MLflow source, the feed with its time zone and the on-call hours; Approve; the final text appears;
  4. through `page.request` (GET only): `/api/projects/{id}/connections` lists `hotel-data` with status `ok`; `/api/projects/{id}/tracking/sources` lists the MLflow source; `/api/projects/{id}/handoff` returns the approved handoff (its `models[0].name` is `hotel-cancel`). The Models, Data and Runs pages are the pages plan's and are not a prerequisite;
  5. open Data Quality (then one more of the four): its session shows a message from "Handoff" and the reply `Audit context included:` with a line containing `bookings_e2e`; Onboarding's session shows no message from "Handoff".
- **Backend in-process flow** (`started_app` from `tests/api/conftest.py`, `FakeDockerRunner`, the fake OpenAI server and the fake MLflow ASGI app in process, signed-in client; the fixture data folder is its own subfolder of `tmp_path`, never a folder that contains the app's home, which connections refuse): the same story through the HTTP API (send, poll the session for the approval part, `POST` the decision, poll), then close the app and start a second one on the same data folder: no kickoff message is repeated and none lost (four "Handoff" messages in total); rejecting a `connections` proposal creates nothing; a second approved handoff starts four more messages.
- Hook re-run on resume: `AgentBuilder.build` runs again when a waiting turn resumes, so the resumed Onboarding turn sees the new connection's summary. The fake does not depend on it; the in-process test asserts it with a second scripted reply that echoes the table line, so a change that stops hook re-rendering is noticed.

**Tests (stop condition):**
- `uv run pytest` (the new tests, the full suite), `uv run ruff check`, `uv run ruff format`, `uv run pyright`.
- `npm ci`, `npm run lint`, `npm run format:check`, `npm test`, `npm run build`, `npm run e2e`: the stage 1 test and the new test pass on Windows and Linux; no folder of the run (data folder, fixture folder, repositories) is left behind.
- CI green on the plan branch commit, every job (Backend with its Windows job, Frontend, E2E, Package).

---

## Interface gaps

None open after the overview's consolidation of 2026-10-28.
