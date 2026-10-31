# Stage 4 — Incidents Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; tasks 1, 2 and 6 also get early PRs (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, and its tests (the task's stop condition).

**Goal:** fired results, silences and the app's own failures become a few incidents (S4 §5): grouped by cause and down the pipeline graph, with a lifecycle, a timeline made of events, a severity decided in code, a page that never waits for a model (S4 §7), the matching agent woken to diagnose (S4 §6), the agents' incident tools and `MonitoringQuery`, Alert Prioritization's run, and the extras last: maintenance windows and snooze.

**Branch:** `plan/stage-4-incidents`.

**Read first:** the [stages 4-6 overview](../2026-10-29-stage-4-6-overview.md) (I22 to I27, I32, I33, "Shared by contracts", "Routes", decided points 4, 6, 10, 15, 19, "Files shared"); do not repeat it here. Then S4 §2 (catching up, untrusted text), §5, §6 (waking, recent changes), §7, §13; `agents/messaging/wakeups.py` `fire_due` (the model for a posted system message and its idempotency), `agents/runtime/sessions.py` (`post`, `_check_agent`), `agents/tools/issues.py` (`get_issue_min_priority`, `PRIORITY_RANKS`), `integrations/github/issues.py` (`create_issue`, `ensure_labels`), `api/routers/tracking.py` (route style), `tests/proposals/samples.py` (handoff fixtures), `tests/providers/fake_clock.py` (`FakeClock`, a `Clock`).

**Consumes** (none of the new ones is on `main` yet):
- contracts task 1: I26 and I27 types (`SignalDraft` with `cleared`, `SignalView`, `IncidentView`, `IncidentPatch`, `HoldScope`, `SignalOutcome`, `IncidentChange`, `IncidentObserver`, `Incidents`, `SeverityDecision`, `SeverityPolicy` and `MediumSeverity`, `Delivery`, `RoutePlan`, `Router`, `WakeLabel`, `AgentWaker`, `diagnoser`), `cause_key`, `ACTIVE_STATUSES`, `stages_of`, `ResultsObserver`, `Results`, `StoredResult`, `parse_source_id` (I22), `ExpectationKey`, `SECOND_DEADLINE_KEY`, `Maintenance` and `NeverSuspended` (I25), `Verdicts` (I31, for `bind_verdicts`), `MonitoringQuery`, `QueryWhat`, `QueryProvider`, `QueryProviders` (`monitoring/query.py`), `MonitoringSettings` and `MAX_WAKES_PER_HOUR`, `JobKind.FOLLOW_UPS`, the tool names, `untrusted()`, `EventLog.page`, `PrObserver`/`PrChange` and `PrSyncObserver.add_observer`, the `inc`, `sig`, `win` prefixes, the stage 4 event types.
- contracts task 2: the tables `signals`, `incidents`, `incident_follow_ups`, `recent_changes`, `agent_wakes`, `maintenance_windows`.
- contracts task 4 (task 7 only): `MAINTENANCE_KIND`, `MaintenancePayload`, `WindowSpec` and the kind's key.
- results task 1 (early PR): `Scheduler.register`, `ScheduledJob`, `in_hours`, `interval_dues`, `Services.clock`. Results task 2 (early PR): the `Results` implementation (`add_observer`, `latest_per_check`). Results task 3 (early PR): `Scheduler.add_sync_observer`. Results task 5 (early PR): `Expectations.states`.
- notifications task 1 (early PR): `Notifier`, `Message`. Notifications task 2 (early PR): `OnCallPeople`.
- On `main`: `AgentSessions.post` (stage 2), `Handoffs.current` (I20), `Tracking.query` (I16), `Page`, `Clock`, `EventLog`, `ProposalKinds`, `get_issue_min_priority` and `PRIORITY_RANKS`, `create_issue` and `ensure_labels`, `Tasks.repo_ref`, `GitHubAuth.token`, `DataSummaries.profile`.

**Produces** (types are contracts'; this plan writes implementations only):
- `IncidentsService` implementing `Incidents` (I26), with `bind_verdicts`, and the task 1 routes (early PR).
- `Waker` implementing `AgentWaker` (I27) and `DiagnosisDispatch`, an `IncidentObserver` (task 2, early PR).
- `SuspectLinker` (task 3).
- `RuleSeverity` implementing `SeverityPolicy`, `OnCallRouter` implementing `Router`, the `Reactor` (task 4).
- The tools `IncidentUpdate`, `IncidentMerge`, `IncidentSplit`, `IncidentFollowUp`, `MonitoringQuery`; the `signals` and `incidents` query providers; the `FOLLOW_UPS` job (task 5). `AdjustSeverity` and Alert Prioritization's wake (task 6, early PR).
- `MaintenanceWindows` implementing `Maintenance`, `MaintenanceKind`, its card, the `windows` provider, the snooze and window routes (task 7).
- Routes: the overview's "Routes" rows for incidents tasks 1 and 7.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | Signals, incidents, lifecycle, observers, timeline, routes | contracts tasks 1, 2; results task 2 (wiring only) | after contracts tasks 1 and 2 and results task 2; early PR |
| 2 | `AgentWaker` and the diagnosing-agent dispatch | task 1 | after task 1; early PR |
| 3 | Grouping, subtype at open, quiet period, reopen, suspects, resolved while offline | task 1; results tasks 3, 5 | after task 2 and results tasks 3 and 5 |
| 4 | Severity, routing, the reactor, the issue threshold | tasks 1 to 3; results task 1; notifications tasks 1, 2 | after task 3 and notifications task 2 |
| 5 | Incident tools, `MonitoringQuery`, the `follow_ups` job | tasks 1, 2; results task 1 | after task 4 |
| 6 | Alert Prioritization's run and `AdjustSeverity` | tasks 2, 4, 5 | after task 5; early PR |
| 7 | Maintenance windows, snooze, the `maintenance` kind and card | task 1; contracts task 4 | after task 6 and contracts task 4 |
| 8 | The Inbox set: `inbox=true` on the incidents list (Decision #576) | task 1; stats task 1 (`bind_verdicts`) | may run before task 7 (ui task 3 waits on it); **early PR** |

Tasks 1 to 6 are the core path; task 7 is the extra (until it merges, contracts' default never suspends anything and no snooze or window route exists).

---

### Task 1: Signals, incidents, lifecycle, observers, timeline, routes

**Files:** new `backend/src/mlops_agents/monitoring/incidents/store.py` (rows ↔ views), `service.py` (`IncidentsService`), `from_results.py` (`SignalsFromResults`, the `ResultsObserver`); new `api/routers/incidents.py`; `api/services.py` (its lines, `Results.add_observer`); tests under `backend/tests/monitoring/incidents/`; regenerated `frontend/openapi.json`.

**Interfaces:** consumes contracts tasks 1, 2 as listed, `Results`, `EventLog` (`record`, `page`), `Maintenance`, `Clock`. Produces `IncidentsService(databases, events, results: Results, severity: SeverityPolicy, maintenance: Maintenance, clock: Clock)` with `bind_verdicts(verdicts)`, and the routes: list (`Page[IncidentView]`), get (404 `incident_not_found`), signals, timeline (`?before=&limit=`, `list[EventRecord]` newest first, through `EventLog.page(subject_id=...)`), acknowledge, close.

**Notes:**
- Every method of `Incidents` is real in this task (no stub): `raise_signal` attaches by cause key to an incident in `ACTIVE_STATUSES` else opens one (task 3 adds the window, the graph and reopen); `snooze` sets `snoozed_until` and records `incident.snoozed` (task 7 adds its route and its effect on paging); `merge` keeps the oldest incident, moves the signals, sets the others `merged` with `merged_into`, and records `incident.merged` on each; `split` moves the named signals to a new incident (`incident.split` on both); `record_hold` stores the scope and reason, sets `held`, records `incident.hold_recorded` and emits change `hold`; `holds(model)` returns the hold scopes of incidents not `resolved`, `closed` or `merged`, of that model or of none.
- `list` ranks by severity (critical first), then newest `detected_at`; the `status` filter defaults to every status.
- `IncidentView.verdict` comes from `Verdicts.get` once `bind_verdicts` was called (stats task 1), else `None`. `signal_ids` are read from `signals`.
- `raise_signal` builds the key only with `cause_key(draft.subject, draft.model)`; asks `SeverityPolicy.decide` (contracts' default until task 4); an attach raises the severity when the decision is higher and never lowers it. `started_at` is the earliest signal's `ran_at`, `detected_at` the first signal's `seen_at`. A new incident's `subtype` is `"silence"` for a silence signal, else `None` (task 3 adds the code rules). A draft whose `result_id` already has a signal returns that signal's outcome and changes nothing (a re-delivered result).
- Observers run in registration order, each awaited before `raise_signal` returns; one that raises is logged and the next still runs; the stored signal is never rolled back.
- `update`: an agent sets only `mitigated` or `resolved` (the patch type allows nothing else); `resolved` is refused (`DomainError`, 409 `incident_still_firing`) while any of `Results.latest_per_check(subject, model)` fires, and, until task 3, always for an incident holding a silence signal. A changed `category` or `subtype` re-decides the severity and emits a `cause` change, and also `severity_raised` (with `before` and `after`) when the severity rose, so a reclassification to critical is paged. Only `acknowledge`, `snooze`, `close` take a person (`by="user"`); no tool calls them.
- Every change records exactly one event of contracts' type with `subject_id` = the incident ID. Free text (a note, a cause) is stored as given and reaches an agent only through `untrusted()`.
- `SignalsFromResults.stored`: for each new record with `fired` true, `origin` not `team` (S4 §10) and `not_enough_data` false, oldest `ran_at` first: skip it when `Maintenance.suspended` holds for its connection (`parse_source_id(record.source_id)`; the result stays stored), else raise a `fired_result` signal (`result_id`, `source_ref` the source ID). Its registration on `Results.add_observer` needs results task 2 on `main`.

**Tests (stop condition)** (temp data folder, `FakeClock`, an in-test `Results` and `SeverityPolicy`, `EventLog` real):
- a fired agent record opens one incident with the right category, key, timestamps and `incident.opened`; a second record with the same subject and model attaches; a `team` record, a non-fired one and a `not_enough_data` one raise nothing; a batch is raised oldest first; the same result delivered twice raises one signal; a record in a suspended scope (fake `Maintenance`) raises nothing.
- each lifecycle transition sets its timestamp and records one event; an agent cannot resolve while a latest result fires and can once it stops; acknowledge and close are routes only; merge sets the absorbed incidents `merged` with `merged_into` and keeps every signal on exactly one incident; split; `holds` returns the open incidents' scopes for the model and the model-less ones.
- a subtype change that raises the severity emits `cause` and `severity_raised`; one that does not emits only `cause`.
- an observer that raises does not stop the next one nor fail `raise_signal`.
- a bound fake `Verdicts` fills `verdict`; unbound gives `None`.
- routes: list ranking, paging and status filter, 404, timeline only of that incident, newest first, paged with `before`; `openapi_export --check` passes; ruff, format, pyright, full suite; CI green.

**PR:** early PR from `plan/stage-4-incidents-task1` into `main` (`AGENTS.md` rule 5): results, notifications, hooks, training, stats and ui consume it.

---

### Task 2: `AgentWaker` and the diagnosing-agent dispatch

**Files:** new `backend/src/mlops_agents/monitoring/waker.py` (`Waker`), `monitoring/incidents/dispatch.py` (`DiagnosisDispatch`); `api/services.py` (its lines); tests.

**Interfaces:** consumes `AgentSessions.post`, `agent_wakes`, `MAX_WAKES_PER_HOUR`, `WakeLabel`, `diagnoser`, `untrusted()`, `Incidents`. Produces `Waker(databases, events, clock)` with `bind(sessions)` (built before the sessions, bound after them, as `wakeups.bind` is) and `DiagnosisDispatch(incidents, waker)`.

**Notes:**
- `wake` order: in one transaction, return if the `(agent, label, subject_id, key)` row exists (already woken), count the project's rows created in the last hour, insert the row with its text and `posted_at` null; then `sessions.post(author_name=<one constant>, from_agent=<the same constant>, text, exchange_id=None, hop=None)`; then set `posted_at`. A post that raises deletes the row so a later call retries. At startup, rows left with `posted_at` null (a crash between insert and update) are posted again once from their stored text: a rare duplicate is accepted, a lost critical wake is not.
- Over `MAX_WAKES_PER_HOUR`: nothing is posted, `incident.wake_skipped` is recorded with `subject_id` = the wake's `subject_id` (the incident for `incident`, `fix_request` and `follow_up` wakes; the promotion or model for `post_deploy`).
- `DiagnosisDispatch.changed` wakes `diagnoser(category, subtype)` with label `incident` and `subject_id` the incident, on `opened` (key `opened`), `reopened` (key `reopened:<reopened_count>`), `severity_raised` (key `severity:<after>`) and a `cause` change that moves the incident to another diagnoser (key `diagnoser:<agent>`; an update that emits both `cause` and `severity_raised` wakes the new diagnoser once, under this key), never for a `resolved_offline` incident. The text names the incident and its severity in plain words; every value taken from a record (subject, detail, cause) goes inside `untrusted(..., source="incident")`.

**Tests (stop condition):** a wake posts one system message to the agent's session (fake `AgentSessions`); the same key twice posts once; the 31st wake in an hour posts nothing and records `incident.wake_skipped` on the incident, and one an hour later posts; a failing post leaves no row and a retry posts; a leftover unposted row is posted at startup; dispatch wakes each category's agent per `diagnoser`, once per key, and not for a resolved-offline incident; text planted in a detail stays inside the untrusted block; checks and CI green.

**PR:** early PR from `plan/stage-4-incidents-task2` into `main` (`AGENTS.md` rule 5): training and promotion wake agents through it.

---

### Task 3: Grouping, subtype at open, quiet period, reopen, suspects, resolved while offline

**Files:** `monitoring/incidents/grouping.py`, `subtype.py`, `suspects.py`, `service.py` and `from_results.py` (their changes); `api/services.py` (its lines); tests.

**Interfaces:** consumes `Handoffs.current` (`payload.pipeline`, `payload.edges`, `models[].commands`, `models[].production_signal`), `stages_of`, `Expectations` through `bind_expectations` (expectations is built after incidents and takes it in its constructor, so incidents gets it back by a bind, as `bind` is used elsewhere), `MonitoringSettings` (`grouping_window_minutes`, `reopen_window_hours`, `recent_change_window_hours`), `Results.latest_per_check`, `Expectations.states` (results task 5), `SyncObserver`, `Scheduler.add_sync_observer` (results task 3), `PrObserver`, `Tracking.query` (`ci_runs`, `deployments`), the `recent_changes` table. Produces `IncidentsService.bind_expectations(expectations)` and `SuspectLinker` (a `SyncObserver` and a `PrObserver`).

**Notes:**
- Join an incident in `ACTIVE_STATUSES` when its cause key matches and its latest signal's `ran_at` is within `grouping_window_minutes`, or when the new subject is downstream of its subject: `stages_of` maps each subject to its stages (none → cause key only), downstream is reachable along `edges` with a visited set (decided point 15). Several candidates: the oldest open incident wins.
- Subtype at open, so severity and `diagnoser` act before any diagnosis: a fired record of a `ci` source whose workflow (its subject) is that of a model's `train` command of type `ci_dispatch` → `failed_training`; of its `deploy` command of type `ci_dispatch` → `bad_deploy`; its `production_signal` of kind `ci_job` → `failed_scoring`; a silence → `silence`; else `None`.
- `resolved` for an incident holding a silence signal: allowed once every expectation its silence signals name is `ok` in `Expectations.states` (through the bound expectations).
- Quiet period: an attach that does not raise the severity returns `change="quiet"` and emits no `IncidentChange`; a raise returns `attached` and emits `severity_raised`.
- Reopen: a signal whose key matches a `resolved` (not `closed`) incident with `ran_at - resolved_at <= reopen_window_hours` reopens it (same ID, `reopened_count + 1`, `incident.reopened`, change `reopened`).
- Resolved while offline (S4 §2): in `SignalsFromResults`, a fired record whose check has a newer stored record for the same subject and model that does not fire (`latest_per_check`) is raised with `cleared=True`: `raise_signal` then opens an incident already `resolved` with `resolved_offline=True`, emits `opened` (the dispatch and the reactor skip it), and it wakes and pages no one.
- Suspects: changes are agents' merged PRs (`PrObserver`), the team's CI runs and deployments (`SyncObserver`, after each sync), each kept in `recent_changes` (unique kind and ref) from its arrival. An incident whose `started_at` falls within `recent_change_window_hours` after a change gets one `incident.suspect_linked` event per change (idempotent by incident and change), on open and on each later change; merged agent PRs go into `IncidentView.prs`.

**Tests (stop condition):** each subtype rule from a handoff fixture (`tests/proposals/samples.py`), and the matching diagnoser woken; a silence incident cannot be resolved while its expectation is `silent` and can once it is `ok`; same key inside and outside the window; a column subject downstream of a schema-change incident joins it, through a cycle without looping, and an unmapped subject groups by key only; a quiet attach notifies no observer, a severity rise does; reopen inside the window keeps the ID, outside opens a new incident, a closed one is never reopened; a catch-up batch (fired at 02:00, cleared at 03:00) opens one resolved-offline incident and wakes no one, while a still-firing one opens normally; a deployment one hour before an incident is linked once across two syncs, one outside the window is not, and a change arriving after the incident opened is linked too; checks and CI green.

---

### Task 4: Severity, routing, the reactor, the issue threshold

**Files:** `monitoring/incidents/severity.py` (`RuleSeverity`), `routing.py` (`OnCallRouter`), `reactor.py`; `api/services.py` (its lines: the real policy replaces contracts' default; the reactor's registration replaces task 2's dispatch line); tests.

**Interfaces:** consumes `OnCallPeople`, `Notifier`, `Message`, `in_hours`, `Handoffs.current` (feeds, models' `profile.cost_ratio`, the graph), `SECOND_DEADLINE_KEY`, `get_issue_min_priority`, `PRIORITY_RANKS`, `create_issue`, `ensure_labels`, `Tasks.repo_ref`, `GitHubAuth.token`, `DiagnosisDispatch`. Produces `RuleSeverity`, `OnCallRouter`, `Reactor` (the one registered incident observer for paging and waking).

**Notes:**
- Severity is code only, never a model (S4 §7), from base `medium`: subtype `failed_scoring` or `bad_deploy` → critical; a silence of a feed or job production depends on (its stage has a path to a stage listing a model, decided point 4, visited set) → high, critical once a signal carries `SECOND_DEADLINE_KEY`; a `model_performance` incident of a model whose `cost_ratio` says a miss costs more → one level up. Scope and confidence have no field in the standard record, so code does not apply them: Alert Prioritization's one-level adjustment covers them (Known debt below). `reasons` names each rule that applied.
- Routing per S4 §7: critical → step 0 to the first on-call person by `escalation_order` who is `in_hours` now, else the first by order, at any hour; later steps are notifications task 4's. High → the first person in hours now, else `morning_report`. Medium and low → `inbox` only. `open_issue` when `PRIORITY_RANKS[severity] >= PRIORITY_RANKS[get_issue_min_priority(...)]`; never retype the literal.
- The reactor, on `opened`, `reopened`, `severity_raised` and `hold`: skip a `resolved_offline` incident and one snoozed at that moment; plan; send every `now` delivery through `Notifier` (kind `incident`; `urgent` for critical; `diagnosis_pending` while there is no cause; a hold's message says what was held), open the issue when planned and not yet opened (once per incident), and only then call `DiagnosisDispatch.changed` (not for `hold`). On `cause` it only calls the dispatch. A notifier or GitHub failure never stops the wake. It is the only sender of immediate deliveries; notifications task 4 sends only the escalation steps.
- The issue: `create_issue` with labels `category:` and `priority:` (as `OpenIssue` does), its number added to the incident's `issues` and recorded as `incident.updated` (changed field `issues`); stage 2's `issue.opened` is not used (the PR reminder reads it). A project without a GitHub remote opens none.

**Tests (stop condition):** each severity rule and its reason; routing at night and in hours (fake clock, a time zone that is not UTC), critical at any hour, high deferred out of hours; a fired record of a model's production-signal CI job, with the real `RuleSeverity` and subtype rule, opens a critical incident that reaches the fake Discord with every provider key exhausted and before the diagnoser is woken (order asserted); a severity rise to high in hours sends once; a quiet attach sends nothing; a notifier failure still wakes; the issue opens at and above the threshold only, once per incident; a snoozed incident sends nothing; checks and CI green.

---

### Task 5: Incident tools, `MonitoringQuery`, the `follow_ups` job

**Files:** `monitoring/incidents/tools.py` (`IncidentUpdate`, `IncidentMerge`, `IncidentSplit`, `IncidentFollowUp`), `monitoring/incidents/query.py` (the `MonitoringQuery` tool, the `signals` and `incidents` providers), `monitoring/incidents/follow_ups.py` (`FollowUpsJob`); `agents/tools/__init__.py` (its lines in `default_registry`); `api/services.py` (its lines); tests.

**Interfaces:** consumes the tool names, `untrusted()`, `QueryProviders`, `ScheduledJob`, `Scheduler.register`, `interval_dues`, `DataSummaries.profile` (`row_count`), `Waker`. Produces the tools (risk class `read`), the two providers, `FollowUpsJob(kind=JobKind.FOLLOW_UPS)`.

**Notes:**
- Tools pass `ctx.agent` as `agent`; their errors are tool errors, never exceptions out of the turn. `IncidentFollowUp(incident_id, after_days?, after_labels?, note)` needs exactly one of the two; `after_labels` needs a model with a `labels` table in the handoff, and stores that table's row count as the base.
- `FollowUpsJob.due` gives one job per collection interval (`interval_dues`); `run` wakes the registering agent (label `follow_up`, subject the incident, key the follow-up row ID) for each follow-up whose days have passed or whose label count reached base + N, and marks it done. Idempotent.
- `MonitoringQuery`: `limit` above 200 is refused; a `what` with no registered provider answers "nothing is recorded for <what> yet"; rows are rendered as JSON inside one `untrusted(..., source="monitoring_query")` block, with `total` and `truncated` outside it.

**Tests (stop condition):** each tool through the registry with a `ToolContext` (update refused while firing, merge, split, follow-up validation); a day follow-up wakes once at its time and a label follow-up once the count reaches base + N; `MonitoringQuery` paging, the cap, an unknown provider, and an instruction planted in a signal's detail stays inside the untrusted block; checks and CI green.

---

### Task 6: Alert Prioritization's run and `AdjustSeverity`

**Files:** `monitoring/incidents/prioritization.py` (`PrioritizationWake`, an `IncidentObserver`), `monitoring/incidents/tools.py` (`AdjustSeverity`); `agents/runtime/sessions.py` and `agents/runtime/contract.py` (`AgentSessions.post`'s `system` keyword and the chat-check exception only); `agents/tools/__init__.py`, `api/services.py` (their lines); tests.

**Interfaces:** consumes `Waker`, `Incidents.set_severity`, `SeverityPolicy.decide`, the tool name `AdjustSeverity`. Produces `PrioritizationWake` (registered after the reactor) and the `AdjustSeverity` tool (risk class `read`).

**Notes:**
- `chat: false` agents refuse every message today (`_check_agent`, `agent_cannot_chat`). `AgentSessions.post` (and `SessionManager.post`) gains a keyword `system: bool = False`; a system post skips the chat check, and only `Waker` passes it. Users and other agents still cannot message a `chat: false` agent. If any other part of the runtime refuses such an agent's turn, open a Decision rather than work around it.
- Alert Prioritization is woken (label `incident`, subject the incident) once per `opened`, `reopened`, `merged`, `split` and per severity change not made by itself (keys `<kind>:<n>`), so its own `AdjustSeverity` never re-wakes it. It runs after the reactor, so paging never waits for it.
- `AdjustSeverity(incident_id, direction, reason)`: refused in code for any agent but Alert Prioritization; moves one level and never more than one level from the latest `SeverityPolicy.decide`; calls `set_severity(by=agent)`, so the change and its re-routing follow the normal path.

**Tests (stop condition):** a system wake reaches a `chat: false` agent and a user message to it is still refused; one wake per change and none after its own adjustment; a second adjustment beyond one level is refused; another agent is refused; the change records `incident.severity_changed` with the reason; checks and CI green.

**PR:** early PR from `plan/stage-4-incidents-task6` into `main` (`AGENTS.md` rule 5): definitions task 1 gives the agents the tools of tasks 5 and 6.

---

### Task 7: Maintenance windows, snooze, the `maintenance` kind and card

**Files:** `monitoring/incidents/maintenance.py` (`MaintenanceWindows`, `MaintenanceKind`, the `windows` provider); `api/routers/incidents.py` (snooze and window routes); `api/services.py` (its lines: the real `Maintenance` replaces contracts' one `NeverSuspended()` line, so every service given it gets the real one; the kind is registered); `frontend/src/pages/agents/proposals/MaintenanceProposalCard.tsx` and its test, one line in `ApprovalCard.tsx`; tests; regenerated `frontend/openapi.json`.

**Interfaces:** consumes `Maintenance`, `MAINTENANCE_KIND`, `MaintenancePayload`, `WindowSpec` and the kind's key, `ProposalKind`, `ProposalKinds.register`, `QueryProviders`. Produces `MaintenanceWindows(Maintenance)`, `MaintenanceKind`, `MaintenanceProposalCard`, the `windows` provider, the snooze and window routes.

**Notes:**
- `suspended(scope, at)`: an `ExpectationKey` matches when its `ref` is in a window's `scope`, a string when it equals a scope entry; `start <= at < end`. Expectations, `SignalsFromResults` and results' source failures already ask it (contracts' default until now).
- `MaintenanceKind`: preview and card show the window, scope and reason; no credentials; `apply` stores the window once per proposal (`win` ID). It asks at every level (I32). A person creates and deletes windows through the routes (`proposal_id` null).
- Snooze: the route sets `snoozed_until`; the reactor already sends nothing for an incident snoozed at that moment; escalation reads `snoozed_until` (notifications task 4).

**Tests (stop condition):** a window suspends a feed's key and a connection only inside its time range; a record in scope raises no signal and is stored; apply twice stores one window; the window routes create, list and delete; the snooze route sets `snoozed_until` and a snoozed incident pages no one; card test (renders the window, approve and reject); `openapi_export --check`; checks and CI green.

---

---

### Task 8: The Inbox set (Decision #576)

Added 2026-10-31 for ui task 3: the Inbox must show the incidents that need a person without the frontend copying a status set the backend owns.

**Files:** `monitoring/incidents/store.py` and `service.py` (the rule, once), `api/routers/incidents.py` (the `inbox` query parameter); tests; regenerated `frontend/openapi.json`.

**Notes:**
- `GET .../incidents?inbox=true` returns `ACTIVE_STATUSES` plus `resolved` incidents with no verdict yet (a verdict on a resolved incident closes it, so the Inbox's verdict buttons must reach those), ranked as the list already is. The rule lives in one place in the incidents service or store, using the verdict binding it has (`bind_verdicts`); `status` and `inbox` together is a 422.
- Without `inbox`, the list behaves as today.

**Tests (stop condition):** each active status and a resolved incident without a verdict are returned; a resolved one with a verdict, a closed and a merged one are not; ranking is kept; `status` with `inbox` is 422; backend suite, ruff, pyright, openapi check; CI green.

**PR:** its own PR into `main` from `plan/stage-4-incidents-task8`, created at this task's last commit (ui task 3 consumes it).

## Known debt added

- Severity applies three of S4 §7's five inputs in code (harm now, silence of what production depends on, cost of error). Scope and confidence need fields the standard result record does not have; until it gets them, Alert Prioritization's one-level adjustment is the only way they change a severity. Recorded in `docs/known-debt.md` with the plan PR.
