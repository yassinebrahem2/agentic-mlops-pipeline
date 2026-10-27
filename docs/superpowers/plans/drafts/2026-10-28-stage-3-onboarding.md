> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Onboarding Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; no task gets an early PR (nothing consumes this plan). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the stage 3 overview (`docs/superpowers/plans/drafts/2026-10-28-stage-3-overview.md`, on `main` once reviewed) first: it holds I13 to I21 and the shared-file rules.

**Goal:** after one conversation with Onboarding and two approvals, the project is set up and the four specialists start their first audits (spec §9 applying both kinds, §10, §11):
- approving a `connections` proposal creates the connections; approving a `handoff` proposal stores the handoff, creates the tracking sources, sets network and level, starts the sync and starts the kickoff;
- the kickoff posts one system-labeled "Handoff" message to each of Data Quality, Model Performance, Retraining and Deployment & Rollback at once, recorded per agent so a restart neither repeats nor loses it;
- the add-project wizard ends with an Onboarding step;
- all seven definitions describe what each agent can now do, and `status_board:` is gone from the definition format;
- an end-to-end test (fake models, a fixture repository and data folder) proves the whole flow.

**Branch:** `plan/stage-3-onboarding`.

**Consumes:** every other stage 3 plan merged on `main`: I13 (stall and `propose`), I14, I15 `Connections`, I16 `Tracking`, I17, I18 `HookRegistry` and the hooks `status_board`, `data_summary`, `tracking_summary`, `knowledge`, I19 `Proposals` and `ProposalKind`, `CredentialSlot`, `ApplyResult`, the payload models `ConnectionsPayload` and `HandoffPayload`, `credentials/project_secrets.py` (`move`, `delete_prefix`), `ProposalKinds`, I20 `Handoffs`, `Connections.inspect`; the tools `SaveNote`, `SearchNotes`, `TrackingHistory`, `Propose`; the fake MLflow server (tracking); the `kickoff` table (contracts' stage 3 migration); stage 2's `AgentSessions.post`, `Sandbox.set_network`, `set_permission_level`, `EventLog.record`.

**Produces:** the `connections` and `handoff` proposal kinds (`onboarding/`), `Kickoff` (`onboarding/kickoff.py`), the seven definitions, the Onboarding wizard step (`/projects/:projectId/onboarding`), the scripted fake models, and the end-to-end tests (backend and Playwright).

**Read first:** spec §9 (Applying the handoff, The kickoff), §10, §11, §15 (Kickoff and End to end); `agents/messaging/wakeups.py` (`fire_due`: how a labeled post is made and guarded against a restart); `agents/runtime/sessions.py` (`post`, `_enqueue`); `agents/runtime/executor.py` (`AgentBuilder.build` runs again when a waiting turn resumes, so hooks are re-rendered after an approval).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The seven definitions, without `status_board` | data-summary (hook registry, `status_board` hook), knowledge, tracking and proposals tool names |
| 2 | The kickoff | contracts (kickoff table), I20 |
| 3 | The `connections` and `handoff` kinds | task 2, I15, I16, I19, I20 |
| 4 | The Onboarding wizard step | task 1 (nothing else: the step posts a chat message) |
| 5 | End to end | tasks 1 to 4, the fake MLflow server |

No task has an early PR; the plan's PR is opened after task 5.

---

### Task 1: The seven definitions, without `status_board`

**Files:**
- `backend/src/mlops_agents/agents/definitions/{orchestrator,onboarding,data-quality,model-performance,retraining,deployment,alert-prioritization}.md` (all seven).
- `backend/src/mlops_agents/agents/catalog.py`: remove `AgentDefinition.status_board` and the transitional treatment of `status_board: true` as the `status_board` hook. the overview (I18 and "Files shared") gives this one removal to this plan; data-summary owns every other edit of `catalog.py` and has merged first.
- Tests: `backend/tests/agents/test_agent_definitions.py`, `backend/tests/agents/test_catalog.py`, and every test that hard-codes the tool set (`STAGE_2_TOOLS` in `backend/tests/conftest.py` and in `test_agent_definitions.py`): grep for `STAGE_2_TOOLS` and `status_board` under `backend/tests` and update each user.

**Interfaces:** Consumes I18 (`HookRegistry.names()`, the catalog validating `context_hooks` against it) and the four tool names. Produces the definitions of spec §11.

**Implementation notes:**
- Frontmatter per the §11 table, exactly:

| Agent | Added tools | `context_hooks` |
|---|---|---|
| orchestrator | SaveNote, SearchNotes, TrackingHistory | status_board, tracking_summary, knowledge |
| onboarding | SaveNote, SearchNotes, TrackingHistory, Propose | data_summary, knowledge |
| data-quality | SaveNote, SearchNotes | data_summary, knowledge |
| model-performance | SaveNote, SearchNotes, TrackingHistory | data_summary, tracking_summary, knowledge |
| retraining | SaveNote, SearchNotes, TrackingHistory | data_summary, tracking_summary, knowledge |
| deployment | SaveNote, SearchNotes, TrackingHistory | tracking_summary, knowledge |
| alert-prioritization | none | none |

- Remove the `status_board:` line from all seven files. Only agents that had `status_board: true` get the hook: read all seven files for it (today the Orchestrator's; do not assume). Onboarding's `max_steps` becomes 60. Update the Onboarding description: it no longer "proposes tests and monitoring plans".
- `AgentDefinition` has `extra="forbid"`, so a leftover `status_board:` line fails the load; the all-definitions test below catches it.
- Prompts describe what each agent can do now, in the existing style (a "What you can do now" list and "How to work"), and nothing it cannot yet do:
  - connections are read-only at `/data/<connection name>` in the sandbox, with `duckdb` and pandas for exploring; never load a whole table into the context (the `data_summary` hook is the overview);
  - the hooks the agent has, and what each gives it; notes (`SaveNote` for what it learns, `SearchNotes` before asking again); `TrackingHistory` where the agent has it;
  - Onboarding: the flow of spec §10 (read the repository and ask; propose `connections`, wait for approval, read only the repository until then; explore the mounted data; propose the `handoff`; missing predictions and labels, and no tracking, each become a PR and a Gap), and that a newer approved proposal replaces the current one;
  - the four audit agents: a message authored "Handoff" starts the first audit of their part; the audit ends with a report (what is good, broken or missing, with evidence), saves notes, and uses the stage 2 tools and gates for PRs and issues; replies stay in their own session;
  - checks and result reading are stage 4 and deploy hooks stage 5: where a prompt mentions them, it says they arrive later. Alert Prioritization only changes its stage wording (it still does not run; no tools or hooks added).
- Nothing in `agent.py`, `sessions.py` or `services.py` changes in this task.
- Prove nothing still reads the removed field: `grep -rn "\.status_board\|status_board:" backend/src` shows only `StatusBoard` (the events service) and the hook's own name. If `agents/runtime/agent.py` still reads `definition.status_board`, data-summary has not finished its change: stop and say so in the plan issue.

**Tests (stop condition):**
- With the real `default_registry`, the real `HookRegistry` and `AgentCatalog.load`, all seven definitions load (no unknown tool, no unknown hook, no leftover field).
- A table-driven test compares every agent's `tools`, `context_hooks` and `limits.max_steps` with the §11 table above, and asserts only the Orchestrator has `status_board` in its hooks.
- `parse_definition` of a file with a `status_board:` line raises `DefinitionError`.
- Prompt checks that guard the "nothing it cannot do yet" rule: no prompt of the seven names a tool the agent lacks; Onboarding is the only prompt that mentions `Propose`; the four audit agents' prompts mention "Handoff".
- Full backend suite; `uv run ruff check`, `uv run ruff format`, `uv run pyright`.

---

### Task 2: The kickoff

**Files:** new `backend/src/mlops_agents/onboarding/__init__.py`, `onboarding/briefs.py`, `onboarding/kickoff.py`; `backend/src/mlops_agents/api/services.py` (your own lines only: build `Kickoff`, call `resume()` once after the sessions exist, add it to `Services`); tests `backend/tests/onboarding/test_kickoff.py`.

**Interfaces:**
- Consumes `AgentSessions.post`, `Handoffs.current` (I20: `HandoffView(proposal_id, approved, payload)`), `ProposalKinds.get`, `Databases`, the `handoff_versions` table (`version` integer, `proposal_id`) and the `kickoff` table (contracts: `handoff_version`, `agent`, `message_id` null until posted, `status` `pending` or `posted`, `posted_at`; key `(handoff_version, agent)`), the `messages` table. `HandoffView` carries no version, so `Kickoff` reads the version of a proposal from `handoff_versions` by `proposal_id`.
- Produces:

```python
KICKOFF_AGENTS: Final[tuple[str, ...]] = ("data-quality", "model-performance", "retraining", "deployment")
AUDIT_BRIEFS: Final[dict[str, str]]    # briefs.py: agent name -> that agent's audit brief; keys == set(KICKOFF_AGENTS)
HANDOFF_AUTHOR: Final = "Handoff"      # the message's author_name, as the wake-ups use "Scheduled wake-up"

class Kickoff:
    def __init__(self, databases: Databases, sessions: AgentSessions, handoffs: Handoffs,
                 kinds: ProposalKinds) -> None: ...
    async def start(self, project_id: str, proposal_id: str, summary: str) -> list[str]: ...  # agents posted now
    async def resume(self) -> None: ...   # startup: finish any kickoff of a project's current handoff
```

**Implementation notes:**
- Posting is exactly stage 2's wake-up post: `sessions.post(project_id, agent, author_name=HANDOFF_AUTHOR, from_agent=agent, text=text, exchange_id=None, hop=None)`. The recipient is its own `from_agent`, there is no exchange, so no hop or token limit applies, nothing wakes Onboarding, and replies stay in the agent's own session. `author_kind` stays what `_enqueue` sets today (`"user"`): the label is the author name, as with wake-ups. Do not touch `sessions.py`.
- The text is `[handoff <proposal_id>]` (the marker, first line), then the summary, then that agent's `AUDIT_BRIEFS` entry. The summary is the `handoff` kind's `preview(payload).body` (one renderer, passed in by the kind's apply): capped, and holds no secret because a payload never does. A fixed cap constant in `briefs.py` applies to the whole message.
- The four are posted with `asyncio.gather`, so the audits start at once. A busy agent session queues the message (stage 2's busy rule); that is not a failure.
- **Exactly once, nothing lost.** Per (project, proposal) one `asyncio.Lock` serializes `start` and `resume`. First `start` writes the four rows with status `pending` (`INSERT ... ON CONFLICT DO NOTHING`), so the intent survives a crash. Then for each agent whose row is `pending`:
  1. look for the marker in that agent's earlier posts (the guard `wakeups.fire_due` uses, for a crash between post and mark): `select messages.id where from_agent == agent and author_name == "Handoff" and parts LIKE '%[handoff <proposal_id>]%'`. Found -> mark the row `posted` with that message ID, do not post;
  2. else post, then mark the row `posted` with the new message ID.
  A row already `posted` is skipped. A failed post is logged with its traceback (`logger.exception`) and the row stays `pending`, so the next `resume` retries it; the other three still go out.
- `resume()` at startup, before the app serves: for every project (list them the way `WakeupService._get_all_project_ids` does; a project whose database cannot be opened is logged and skipped), read `Handoffs.current`; when one exists, run the loop above (inserting any missing row as `pending` first). This closes both windows: a crash after the handoff was stored and before any row was written, and one between a post and its mark. Rebuild the summary from the stored payload through the `handoff` kind's `preview` (`ProposalKinds.get("handoff")`), so a restart needs nothing the process lost.
- Each new approved handoff (a later proposal ID) starts its own kickoff, as §9 says applying the handoff starts it; the brief tells the agent to re-audit what the new handoff changes. State this in the PR description.
- An agent with no usable model: the post succeeds, the turn is refused with the stage 1 message in that agent's session, and the row stays. The user fixes the model and asks again; the kickoff never silently repeats.

**Tests (stop condition)** (a real `SessionManager` or the stage 2 fake sessions in `tests/api/fake_sessions.py`, a real project database):
- `start` posts four messages, one per kickoff agent, each `author_name == "Handoff"`, `from_agent == agent`, `exchange_id` and `hop` null, containing the marker, the summary and that agent's brief; four rows `posted`; the call returns the four names.
- A second `start` for the same proposal posts nothing.
- Crash windows: post succeeded but the row still `pending` → `resume` marks it `posted` and posts nothing; handoff stored but no rows and no messages → `resume` writes them and posts four; one agent's post raising → the other three are posted and recorded, and the next `resume` posts only the pending one.
- A restart (a new `Kickoff` and `SessionManager` over the same data folder) neither repeats nor loses a kickoff.
- A newer approved handoff posts four more messages with its own marker.
- Onboarding's session gets no message from the kickoff, and replies to the four do not create an exchange or wake Onboarding.
- `set(AUDIT_BRIEFS) == set(KICKOFF_AGENTS)`, and every name exists in `AgentCatalog.load(...)` with `chat: true`.
- Full suite and checks.

---

### Task 3: The `connections` and `handoff` kinds

**Files:** new `onboarding/connections_kind.py`, `onboarding/handoff_kind.py`, `onboarding/handoff_parts.py` (one function per part of the apply, kept out of the kind so each stays small); `api/services.py` (your own lines: construct the two kinds with their services and register them); tests `backend/tests/onboarding/test_connections_kind.py`, `test_handoff_kind.py`.

**Interfaces:**
- Consumes `ProposalKind`, `CredentialSlot`, `ApplyResult`, `ConnectionsPayload`, `HandoffPayload` (contracts' `proposals/payloads.py`: `ConnectionsPayload.connections: list[ConnectionSpec(kind, name, settings, why)]`; `HandoffPayload.profile`, `data_roles: list[DataRole(connection, table, role, ...)]`, `tracking: list[TrackingSourceSpec(type, settings, auth)]`, `pipeline`, `commands`, `operations`, `gaps`, `grants: Grants(network, permission_level)`, where `permission_level` is a `GrantableLevel`, never `bypass`), `Connections.list/create/inspect/tables` (I15: `inspect(project_id, kind, settings) -> Findings`), `Tracking.sources/create_source/sync` (I16), `Handoffs.store` (I20), `project_secrets.move(src_ref, dst_ref)` and `delete_prefix(prefix)` (contracts), `ProposalKinds.register` (proposals), `Sandbox.set_network`, `set_permission_level(databases, project_id, level)`, `EventLog.record`, `Kickoff.start`.
- Produces two `ProposalKind` classes, `ConnectionsKind` (`name = "connections"`) and `HandoffKind` (`name = "handoff"`), each with `payload_model`, `preview`, `credentials_needed` and `apply`; `apply` is idempotent as I19 requires.

**Implementation notes:**
- **`ConnectionsKind`.**
  - `preview`: `Preview(kind="text", title="Connections", body=...)`: per entry, name, kind, settings and its `why`; for a `local_folder`, the path. `preview` is pure, sync and cheap (the `Propose` tool calls it twice per call), so it holds no findings: the card shows the denylist warning by calling `POST .../connections/inspect` (see Interface gaps).
  - `credentials_needed`: none (no connection kind in this stage needs one).
  - `apply`: one `Connections.create(project_id, kind, name, settings)` per entry, in order, each its own part named `connection:<name>`. Validation errors (a folder inside the app's data folder, a missing path) are `DomainError`s with user-facing messages: reported under `failed`, the rest applied. Idempotent: a connection of that name with the same kind and settings already existing counts as applied; the same name with other settings fails with "a connection named X already exists with other settings".
- **`HandoffKind`.**
  - `preview`: one readable `Preview(kind="text", ...)` of profile, roles, tracking sources, pipeline, commands, operations (checks), gaps and grants. The same body is the kickoff summary, so there is one renderer. The network sentence of §7 (an unauthenticated tracker can be changed by agents at `autonomous` and `bypass`) is part of the grants block when an `mlflow` source has `auth == "none"` and network is on (a `github_actions` source is always `auth: "none"` and uses the app's GitHub login, so it never triggers it).
  - `credentials_needed`: one `CredentialSlot(index=i, label=..., source_type=...)` per tracking source with `auth == "credential"`, `i` being the source's position in the payload.
  - `apply` runs these parts in order, each wrapped by one helper that records `applied.append(part)` or `failed[part] = reason` and never lets one part stop the next. The helper catches `DomainError` (its message is the reason) and, at this third-party-free boundary only, `Exception` for the unexpected: it logs the traceback and records the generic reason "unexpected error, see the log"; reasons pass through `scrub_values` with the project's secrets before they are stored. No bare `except`.

| Part | Does | Depends on |
|---|---|---|
| `handoff` | checks every data role names an existing connection and one of its tables (`Connections.list`, `.tables`), then `Handoffs.store(project_id, proposal_id, payload)` | none; a role naming a missing connection fails the part ("needs the connection X first") and nothing is stored |
| `tracking:<i>` | `Tracking.create_source(project_id, type, settings, auth)`; for `auth == "credential"` then `project_secrets.move("project/<id>/proposal/<prp>/<i>", "project/<id>/tracking/<trk>")`; a slot never filled leaves the source created with `credential_set` false (the Runs page shows the sync error) | none; one source failing does not stop the others |
| `network` | `Sandbox.set_network(project_id, grants.network)` | none |
| `level` | `set_permission_level(databases, project_id, grants.permission_level)`; `bypass` is refused here too (`failed["level"] = "a proposal cannot set bypass"`), even though the payload model forbids it, and the level is left unchanged | none |
| `sync` | `Tracking.sync(project_id)`, which only starts a sync (I16); `tracking.synced` and the source's `last_error` report its end, not this result | at least one `tracking:*` applied |
| `event` | `EventLog.record(project_id, source="onboarding", type="handoff.approved", subject_id=proposal_id, payload={"proposal_id": proposal_id})` (no payload field beyond the ID) | `handoff` applied |
| `kickoff` | `Kickoff.start(project_id, proposal_id, summary)` | `handoff` applied; otherwise `failed["kickoff"] = "not started: the handoff was not stored"` |

  - `apply` is idempotent: running it again for the same proposal re-stores the same handoff version (`Handoffs.store` is keyed by the proposal), skips a tracking source whose settings match one of the project's existing sources, and `Kickoff.start` posts nothing twice.
  - Credentials: after the `tracking:*` parts, in a `finally`, `project_secrets.delete_prefix("project/<id>/proposal/<prp>/")` removes every slot secret still there (a source that failed to create), as §9 requires; reject, cancel and expiry are the proposals observer's. Secrets already moved are untouched.
  - No secret appears in a preview, an `ApplyResult`, an event or a log line: slot references are built from IDs only, and the credential value is never read in this plan.
- Registration: `build_services` registers both kinds with `ProposalKinds.register` (proposals builds the registry first); the kinds hold injected services, never look them up. Re-applying an approved proposal with no `result` after a restart is proposals' (I19); it is why `apply` is idempotent.

**Tests (stop condition)** (real `Databases`, real or fake `Connections`/`Tracking` as the producing plans' own fakes allow, the fake MLflow server for a real source):
- Connections: approving two entries creates both; a folder inside the data folder fails and the other is created; the same call twice creates one connection; same name with other settings fails and leaves the existing connection.
- Handoff: all parts applied → `applied` lists them all, the handoff is current, the sources exist, network and level are set, `handoff.approved` is recorded, and the kickoff posted four messages.
- A tracking source whose creation raises: `failed["tracking:1"]` has the reason, the other parts are applied, the kickoff still starts.
- A role naming a missing connection: `failed["handoff"]`, nothing stored, the kickoff and the event not run, network, level and the sources still applied.
- `bypass` in the payload (bypassing the model through a hand-built object): the level part fails and the level stays.
- A credential: with a slot filled before apply, the secret is readable at `project/<id>/tracking/<trk>` and gone from the slot reference afterwards; a slot of a source that failed to create is deleted by `apply`; the approval preview, `ApplyResult`, the events and the audit rows contain neither the value nor anything of it (assert on the serialized text).
- Running `apply` twice: one current handoff version per proposal, one source, four kickoff messages in total.
- `preview` of a handoff with an unauthenticated tracker and network on contains the network sentence; with network off it does not.
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
- `backend/tests/fakes/onboarding_script.py` (new: `onboarding_reply(messages, tools)` and `audit_reply(messages)`), and in `backend/tests/fakes/fake_openai.py` two entries in `MODELS` (`fake-onboarding`, `fake-audit`) and two dispatch lines in `_reply`, placed right after the `report_capabilities` branch where `fake-write` sits (the generic "last message is a tool result" branch below ends any script with "Done. First line: ...", so a later position would stop `fake-onboarding` after its first `Propose`): the overview lets onboarding add its scripted models there, in its own functions; nothing else in that file changes.
- `backend/tests/fakes/test_onboarding_script.py` (the scripted payloads validate against the real `ConnectionsPayload` and `HandoffPayload`, so a contracts change fails here, not in the browser).
- `backend/tests/onboarding/test_onboarding_flow.py` (in-process end to end, including a restart).
- `frontend/e2e/flow.spec.ts` (a second test; the stage 1 test is untouched except task 4's line), `frontend/e2e/repo.ts` (`createDataFolder()`), `frontend/e2e/env.ts`, `frontend/e2e/run.mjs`, `frontend/playwright.config.ts` (the fake MLflow server as a third `webServer`).

**Interfaces:** Consumes everything above and the tracking plan's fake MLflow server. Produces the end-to-end tests and the two scripted fake models.

**Implementation notes:**
- **Scripted models.** No Bash and no Docker anywhere in the scripts (e2e CI has no Docker). The fake derives its step from the conversation, never from server state, so tests stay independent:
  - `fake-onboarding` (must answer the verify step like any fake model: that branch comes first in `_reply`):
    - no user message contains `DATA_PATH=` → plain text asking where the data lives (this answers the wizard's first message);
    - `DATA_PATH=<path>` present and no tool result yet → `Propose(kind="connections", payload=...)` with one `local_folder` connection named `hotel-data` at that path (the value runs to the end of its line);
    - one tool result → `Propose(kind="handoff", payload=...)` with: a profile (objective, metric `f1`), one role (`training` for table `bookings_e2e`), one MLflow tracking source at the URL given as `MLFLOW_URL=<url>` in the same user message with `auth: "none"`, one pipeline stage, grants `network: false` and `permission_level: "ask"`, and every other required field of the contracts' models filled;
    - two tool results → a final text ("Onboarding is complete.").
    Payloads are built as Python dicts and serialized with `json.dumps` (a Windows path's backslashes must survive).
  - `fake-audit`: no tools; it searches every message's content (a string or a list of parts) for the first line containing `bookings_e2e` and answers `Audit context included: <that line, cut at 160 characters>`; with no such line it answers `Audit saw no data.` The assertion on this text proves the `data_summary` hook reached the turn's context.
- **Fixtures.** The data folder holds `bookings_e2e.csv` (a header and 7 rows) and one unsupported file (`notes.txt`). It lives in the OS temp folder under its own prefix, never inside `E2E_DATA_DIR` (connections refuse paths inside the app's data folder). On Windows DuckDB or the sync may still hold files when a spec ends, so (as Decision #143 did for the data folder) `run.mjs` makes the folder once, passes it as `E2E_FIXTURE_DIR`, and removes it after Playwright exits, with the same guard: only a folder named with the prefix, directly in the OS temp folder, real paths compared. Factor the guard so both removals use one function (no copy). The project repository is a second `createTestRepo()`.
- **Fake MLflow.** The tracking plan's fake server (`python -m tests.fakes.fake_mlflow --port <n>`, I16) is started as a third Playwright `webServer` on a new port constant in `e2e/env.ts` (`MLFLOW_PORT`, `FAKE_MLFLOW_URL`) next to the two existing ones; the first sync succeeds with the server's seeded experiment. The backend test serves the same fake's ASGI app in process the way `start_fake_server` does for the OpenAI fake.
- **Playwright flow** (a second `test`, its own repository, its own random provider name; `workers: 1` is already set):
  1. sign in; add the fake as a self-hosted provider; add models `fake-onboarding` and `fake-audit` and verify both; on the AI providers page set `fake-onboarding` as Onboarding's model and `fake-audit` as the model of Data Quality, Model Performance, Retraining and Deployment & Rollback (the existing "Model for <agent>" comboboxes);
  2. add the repository as a local project: the Onboarding step opens; its model row is ready; Start; the Onboarding session opens with the wizard's message and the fake's question;
  3. send `DATA_PATH=<path>` and `MLFLOW_URL=<url>` in one message; a `connections` approval card shows `hotel-data` and the folder path; Approve; the turn continues (it is a resumed turn, so the `data_summary` hook is re-rendered); a `handoff` approval card shows the profile, the `bookings_e2e` role and the MLflow source; Approve; the final text appears;
  4. through `page.request` (GET only): `/api/projects/{id}/connections` lists `hotel-data` with status `ok`; `/api/projects/{id}/tracking/sources` lists the MLflow source; `/api/projects/{id}/handoff` returns the approved handoff. The Models, Data and Runs pages are the pages plan's and are not a prerequisite;
  5. open Data Quality (then one more of the four): its session shows a message from "Handoff" and the reply `Audit context included:` with a line containing `bookings_e2e`; Onboarding's session shows no message from "Handoff".
- **Backend in-process flow** (`started_app` from `tests/api/conftest.py`, `FakeDockerRunner`, the fake OpenAI server and the fake MLflow ASGI app in process, signed-in client; the fixture data folder is its own subfolder of `tmp_path`, never a folder that contains the app's home, which connections refuse): the same story through the HTTP API (send, poll the session for the approval part, `POST` the decision, poll), then close the app and start a second one on the same data folder: no kickoff message is repeated and none lost (four "Handoff" messages in total); rejecting a `connections` proposal creates nothing; a second approved handoff starts four more messages.
- Hook re-run on resume: `AgentBuilder.build` runs again when a waiting turn resumes, so the resumed Onboarding turn sees the new connection's summary. The fake does not depend on it; the in-process test asserts it with a second scripted reply that echoes the table line, so a change that stops hook re-rendering is noticed.

**Tests (stop condition):**
- `uv run pytest` (the new tests, the full suite), `uv run ruff check`, `uv run ruff format`, `uv run pyright`.
- `npm ci`, `npm run lint`, `npm run format:check`, `npm test`, `npm run build`, `npm run e2e`: the stage 1 test and the new test pass on Windows and Linux; no folder of the run (data folder, fixture folder, repositories) is left behind.
- CI green on the plan branch commit, every job (Backend with its Windows job, Frontend, E2E, Package).

---

## Interface gaps

Still open after the overview's revision of 2026-10-28 and the contracts and proposals drafts; none invented:
1. **The connections card does not show findings.** The proposals draft's `ConnectionsProposalCard` renders each connection and its `why` but never calls `POST .../connections/inspect`, so the denylist warning of §4 is missing from the proposal card. Needed in the proposals plan's card task: the card calls the inspect route per entry and shows `secret_files` and `unsupported_files`. This plan's `preview` is pure and holds none.
