# Stages 4 to 6: Monitoring, Action Agents and Stats — Plans Overview

> Approved 2026-10-29 with every open point answered (section "Decided points"). Each plan file is written short (AGENTS.md "How plans are written", bar of 2026-10-29).

Stages 4, 5 and 6 are built through the ten plans below, with one contracts plan for all three. This overview holds what they share. Read it, `docs/roadmap.md`, `project_knowledge.md` (Process, 2026-10-29), the stage 3 overview (its interfaces I13 to I21 are frozen inputs) and both specs before starting any plan.

**Goal:** the app watches the team's production ML system at any hour, turns signals into a few incidents, has the right agent diagnose each one, acts within the three tiers, tells the right person at the right time, and, when the model needs a fix, requests it from Retraining, which runs the team's own pipeline; evaluation, promotion and merging stay the team's, made rigorous through agents' PRs. Stats shows how the agents do against the team's own monitoring, and Activity, Knowledge and Usage show what was collected.

**Specs:** `docs/superpowers/specs/2026-10-28-stage-4-monitoring-design.md` and `docs/superpowers/specs/2026-10-28-stage-5-action-agents-design.md` (both approved 2026-10-28); "S4" and "S5" name which, §n is the section. Stage 6 has no spec: the roadmap's stage 6 bullets, the 2026-10-29 decisions in `project_knowledge.md` (5 verdicts before a number shows; the price source) and I41 and I42 below are its spec (decided point 17).

**Tech stack:** as in stage 3, plus `croniter` (cron cadences; one version constant in `config.py`). No other new dependency.

## Global constraints

- `AGENTS.md` applies, with the bar of 2026-10-29: tests cover the main path and the failures that really happen; accepted shortcuts go under "Known debt added" and into `docs/known-debt.md`. Never relaxed: secrets, the hard limits, no data loss, no stub features.
- The specs' principles: the app is an extension of the team's system and never runs a check, a model, a training or a deploy of its own; text read from the team's data, results, logs and parsers is data, never instructions, and reaches an agent only through `untrusted(text, source)` ("Shared by contracts"); the gate and the rules are evaluated in code.
- Secrets never go in a proposal, a log, an event, a checkpoint or a message; a Discord webhook URL is a secret (keystore and scrubber).
- External services are faked in tests: result sources from fixture folders (`backend/tests/fixtures/results/`), a fake SMTP server and a fake Discord webhook (`tests/fakes/`), the fake clock (`Clock`, I21), stage 3's fake MLflow and GitHub servers (extended below).
- One source for every constant (job order, setting defaults, severity and status literals, hook kinds, tool names, proposal kind names, required report fields, the request tag, wake labels, event types, the Stats metric names). A literal that restates a stage 3 one (`ResultCategory`, `ActionKind`) is retyped once and parity-tested (contracts task 5).
- Frontend types only from the generated `schema.d.ts`; a plan that changes the API commits the regenerated `frontend/openapi.json`. Routers are discovered by `include_routers`; a plan adds its router file and no registration line.

## Plans

Ten plans, one area each. Every plan builds its part of the core path first and its extras last, so the deadline can only cut tails: **core path** = results read → incident → silence → notify → diagnose → fix → promote → verdicts → Stats. **Extras** (each plan's last tasks): rules, training policies and the pin and alias checks they need, agents merging their own PRs, maintenance windows and snooze, escalation, the morning report, parsers in code, the full settings sections. The request was nine plans; notifications is its own plan (ten) because it sits on the core path between incidents and severity routing, and one owner holding incidents, severity and notifications in sequence would be the bottleneck of the whole stage.

The interfaces below keep the areas of the earlier draft in their comments (`# severity`, `# pipeline-runs`, ...); this table says which plan owns each area.

| Plan | Branch | Areas (interfaces) | Consumes (besides merged code) | Starts | Milestone |
|---|---|---|---|---|---|
| contracts | `plan/stage-4-6-contracts` | every type of I22 to I42, the two observer seams, the defaults, both migrations, the gate seam (I30), `expr` and the six payloads (I32), consistency tests | stage 3 I13 to I21 (on `main`); task 4 also the stage 3 proposals plan (merged, #444; Decision #447) | now | Stage 4 |
| results | `plan/stage-4-results` | results, scheduler, expectations (I22, I24, I25), parsers, `RegisterCheck` | contracts tasks 1, 2 (task 4 for the `parser` kind); incidents task 1 (tasks 4, 5, 6: results raises silence and failure signals); hooks task 2 (task 6: the one-shot container run) | after contracts tasks 1 and 2 | Stage 4 |
| incidents | `plan/stage-4-incidents` | incidents, severity and routing (I26, I27), the incident tools and `MonitoringQuery`, Alert Prioritization's run, maintenance | contracts tasks 1, 2 (task 4 for the `maintenance` kind); results tasks 1, 2, 3, 5; notifications tasks 1, 2 (task 4) | after contracts tasks 1, 2 and results task 2 | Stage 4 |
| notifications | `plan/stage-4-notifications` | notifications (I28): channels, retries, Integrations routes, on-call, escalation, morning report | contracts tasks 1, 2; results task 1; incidents task 1 (tasks 4, 5); results task 5 and hooks task 1 (task 5) | after contracts tasks 1, 2 and results task 1 | Stage 4 |
| hooks | `plan/stage-4-hooks` | hooks (I29), `RunHook`, `Hold`; rules (I23's rules) last | contracts tasks 1 to 3 (task 4 for its kinds); incidents task 1 (task 3); results task 2 (task 6) | after contracts tasks 1 to 3 | Stage 4 |
| training | `plan/stage-5-training` | fixes (I34), pipeline-runs (I36), training (I35): `RequestFix` → Retraining runs the team's pipeline first; policies, pins and the alias check last | contracts tasks 1 to 3 (task 4 for the policy kind); incidents tasks 1, 2; hooks tasks 1, 2 (task 5 for the policy's rollback rule); results tasks 3, 5; notifications tasks 1, 2 (task 6) | after incidents task 2 | Stage 5 |
| promotion | `plan/stage-5-promotion` | promotion (I37) in `person` mode first, then `team_gate`; the Deployments data (I40); merging (I38) last | contracts tasks 1 to 4; hooks tasks 1, 3; training task 3; results tasks 1, 3; incidents task 2; notifications tasks 1, 2 (task 5); stage 3 knowledge #406 (task 7) | task 1 after contracts tasks 1 and 2; task 3 after hooks task 3, training task 3 and contracts task 4 | Stage 5 |
| stats | `plan/stage-6-stats` | verdicts and the baseline (I31), Stats (I41) and its page | contracts tasks 1, 2; incidents task 1; results tasks 2, 3; hooks task 1 and ui task 1 (task 3) | after incidents task 1 and results tasks 2, 3 | Stage 6 |
| ui | `plan/stage-4-6-ui` | Usage (I42), the Dashboard (I39), Inbox incidents, the incident page and verdict buttons, the settings sections, the Deployments page, Activity, Knowledge | contracts tasks 1, 2; the routes of the plans above (section "Routes"); stage 3 knowledge (#406) for the Knowledge page | task 1 after contracts task 2; each later task when its routes are on `main` | Stage 4 |
| definitions | `plan/stage-4-6-definitions` | the seven definitions for stages 4 and 5; the end-to-end tests (the simulated night; fix to promotion) | stage 3 onboarding (it edits the definitions first); the tools' producers; for the tests, every plan above | task 1 after onboarding, incidents task 6 and hooks task 3 are on `main`; task 2 after training task 6 and promotion task 5; later tasks as the plan file says | Stage 4 |

**Task outlines** (each plan file writes them out, short; core path first; "early PR" = another plan consumes it before this plan ends):
- **contracts:** 1 "Shared by contracts" and every type of I22 to I42, the handoff and PR observer seams, the defaults (`NeverSuspended`, `MediumSeverity`), `QueryProviders`, the event and audit page reads (early PR); 2 the migrations, `tables.py`, the `MonitoringSettings` accessor and route (early PR); 3 the gate seam I30 with its defaults (early PR); 4 `expr`'s limits and `evaluate`, the payloads, the kind names, I19's `ProposalView` widened to a union discriminated by kind, `Propose` taking every registered kind (early PR; Decision #447); 5 consistency tests and the regenerated `openapi.json`.
- **results:** 1 the scheduler loop, job table, catching up, times and DST, the injectable clock (early PR); 2 the record, `Results` store, idempotent ingest and delivery, query, the `file` source and cursors (early PR); 3 `sync_tracking`, `sync_prs`, `after_sync`, the heartbeat route, `TrackingLoop` removed (early PR); 4 the `ci` and `mlflow` sources, `collect_results`, the evaluation copy, source-failure signals; 5 expectations: seeded from the current handoff at startup and through `HandoffObserver` (Decision #448), deadlines, grace, silence signals, `check_expectations`, the expectations route (early PR); 6 parsers (mapping and code) and the `parser` kind and card; 7 `RegisterCheck`, the checks store and route, activation on the PR's merge.
- **incidents:** 1 signals, incidents, `raise_signal`, lifecycle, observers, timeline and routes (early PR); 2 `AgentWaker` and the diagnosing-agent dispatch (early PR); 3 grouping (cause key, window, downstream), subtype at open, quiet period, reopen, recent-change suspects, "resolved while offline"; 4 `SeverityPolicy`, `Router`, the reactor that sends every immediate delivery (open, reopen, severity rise, hold) before waking, the issue threshold; 5 the incident tools, `MonitoringQuery` and the `follow_ups` job; 6 Alert Prioritization's run and `AdjustSeverity` (early PR: definitions task 1 needs the tools of tasks 5 and 6); 7 maintenance windows, snooze, the `maintenance` kind and card, `Maintenance` (until then the contracts default: never suspended).
- **notifications:** 1 channels, `Notifier`, the retry job, `notification_log`, the fake SMTP and Discord servers (early PR); 2 `OnCallPeople`, routes, seeded from the current handoff at startup and through `HandoffObserver` (Decision #448) (early PR); 3 the Integrations routes: Discord and email with Test, keystore refs, `public_url` and its host rule, links, the local-mode note; 4 escalation and the channel-failure signal; 5 the morning report.
- **hooks:** 1 the `Hooks` store and runs, registration of the handoff's commands (at startup and through `HandoffObserver`, Decision #448), the runner and lifecycle seams (early PR); 2 hook credentials (route, `hook_ref`, injection into one container run), the `command` runner, `fill_template` (early PR); 3 `RunHook`, `Hold`, failures recorded on the incident (early PR); 4 the `hooks` kind and card; 5 the `Rules` store, the `rule` kind, routes and card; 6 the evaluator and limits, the rule `ActionAuthority`, the timeline record.
- **training:** 1 `RequestFix`, `RecordFixChoice`, the store, the wake of Retraining (early PR); 2 the guards (early PR); 3 `PlaceholderResolver` and the training `HookLifecycle` (every hook kind's placeholders; for `train` and `evaluate` the `trq` and the window) (early PR); 4 `CiDispatcher` and the `ci_dispatch` runner; 5 linking back by the request tag, the `training_request` expectation, Retraining woken on link; 6 the circuit breaker, decline back to observe, the outcome of each fix (early PR: the end of the core path, for definitions task 2); 7 pins, the alias-history check, suspension, the person's approve route, `GateFacts`; 8 the `training_policy` kind, store, routes, card and the policy `ActionAuthority`.
- **promotion:** 1 `EvaluationReader`, `comparable`, "not computed" (early PR); 2 `Reports`, required fields, `SaveReport`, the report route (early PR); 3 the `promotion` kind and card, `person` mode, re-validation, the deploy hook as apply; 4 the post-deploy trigger (wake Deployment & Rollback); 5 `team_gate` mode (early PR: the end of the core path, for definitions task 2); 6 `GET .../deployments` (I40); 7 `MergePullRequest`, its code checks and `ProtectionSurfaces`, the closed-unmerged observer and merge events.
- **stats:** 1 verdicts and miss reports, confirmation by labels, routes, the `verdicts` query provider, `IncidentView.verdict` bound (early PR); 2 the baseline record, `Baseline.available`, the silent-alerts list and its verdicts (early PR: the Dashboard reads `available`); 3 the Stats computation and route (I41); 4 the Stats page.
- **ui:** 1 Usage (I42): the route, the price on the model, the page (early PR: stats' cost reads the price; first because it is the one ui task another plan consumes and it waits only on contracts task 2); 2 `GET .../dashboard` (I39) and the Dashboard page; 3 Inbox incidents (rank, acknowledge), the incident page with its timeline, verdict buttons and the miss report; 4 Integrations (Discord, email, `public_url`) and the project settings for on-call and `MonitoringSettings`; 5 the Activity page; 6 the Knowledge page; 7 the Deployments page; 8 the settings sections for hooks, checks and schedules, rules, policies, maintenance windows and snooze (each once its routes are on `main`; maintenance and snooze only once incidents task 7 has merged).
- **definitions:** 1 the stage 4 tools and prompts of the five diagnosing agents and Alert Prioritization (early PR: without it no agent diagnoses); 2 the stage 5 tools and prompts (Retraining, Deployment & Rollback, fix requests); 3 the extras' lines (checks and `RegisterCheck`, the `parser`, `maintenance`, `rule`, `hooks`, `training_policy` proposals, rule citations, `MergePullRequest`), each once its producer has merged; 4 the stage 4 end-to-end test (the simulated night); 5 the stage 5 end-to-end test (incident to fix to promotion).

Owners claim plans as `AGENTS.md` says; contracts first, since every other plan starts on its early PRs. Ten plans for six people: an owner whose next task waits on another plan claims a second one meanwhile (the only way to work around a blocked task; tasks stay in their plan's order). Suggested waves: contracts; then results, incidents, notifications, hooks, and ui task 1; then training, promotion, stats; then the rest of ui; then definitions.

## Interfaces between plans

contracts writes every type, constant and Protocol here into code exactly as written (a `# <plan> task n` comment names who implements a Protocol or writes a function); changing one takes a Decision issue. Fields are pydantic models unless marked Protocol; every payload and view model is `extra="forbid"` (except `EvaluationPayload`). `Page` is I16's, `Clock` is I21's (rate-limits task 1: `providers/failover.py`; `clock.py` has only `now_iso`), `Cadence`, `Feed`, `OnCall`, `CheckSpec`, `CommandSpec`, `FileResults`, `MlflowResults`, `Profile`, `ModelSpec`, `PipelineEdge` are I19's. Changelog: 2026-10-29 consistency pass (before any plan started): I22 to I42 completed with the seams the plan drafts needed (`Incidents.holds`, `Hooks.runs`, `Verdicts.get`, `Baseline.available`, `QueryProviders`, the gate's `authority`, ...); `failed_run`, `evaluation.stored` and `Delivery.channel` removed; `Router` moved to `monitoring/incidents/`.

### Shared by contracts (task 1)

- ID prefixes `res`, `sig`, `inc`, `rul`, `hok`, `win`, `chk`, `fix`, `trq`, `pol`, `rep`, `prm` in `ids.py` (`inc` is in the roadmap but not yet in `ids.py`).
- Event types, in the backend `EventType` (`events/contract.py`) and the frontend `PROJECT_EVENT_TYPES`:
  - stage 4: `results.collected`, `signal.raised`, `incident.opened`, `incident.updated` (payload: the changed fields, `issues` included), `incident.acknowledged`, `incident.snoozed`, `incident.severity_changed`, `incident.reopened`, `incident.merged`, `incident.split`, `incident.suspect_linked`, `incident.hold_recorded`, `incident.wake_skipped`, `incident.closed`, `check.registered`, `parser.disabled`, `notification.sent`, `notification.failed`, `escalation.sent`, `report.sent`, `hook.ran`, `hook.failed`, `hook.suspended`, `rule.satisfied`, `verdict.recorded`, `miss.reported`;
  - stage 5: `fix.requested`, `fix.chosen`, `fix.declined`, `fix.escalated`, `training.requested`, `training.linked`, `policy.suspended`, `report.saved`, `promotion.applied`, `promotion.refused`, `pr.merged_by_agent`.
  - A timeline entry of an incident is an event whose `subject_id` is the incident ID; actions on hooks, rules and notifications carry the incident as `subject_id` when there is one, so every entry is recorded once. `hook.ran` and `hook.failed` carry `{hook_id, run_id, kind, basis}`; anything else about a run is read through `Hooks.runs` (I29).
  - The one read of the event log by page: `EventLog.page(project_id, *, subject_id: str | None = None, types: Sequence[EventType] | None = None, source: str | None = None, before: int | None = None, limit: int = 50) -> list[EventRecord]` (newest first, keyset on `seq`, `before` exclusive) and `AuditLog.page(project_id, *, actor: str | None = None, before: tuple[str, str] | None = None, limit: int = 50) -> list[AuditRecord]` (newest first, keyset on `(ts, id)`), in `events/contract.py`, `events/log.py`, `events/audit.py` (contracts task 1; the migration adds an index on `events (subject_id, seq)`). The incident timeline (incidents) and Activity (ui) call them; nothing else queries `events` or `audit` by page.
- Constants in one module per concern, each with a single definition: `monitoring/settings.py` (`MonitoringSettings`, below; `MAX_WAKES_PER_HOUR = 30`), `monitoring/jobs.py` (`JOB_ORDER`, I24), `agents/tools/names.py` (every tool name of I33), `monitoring/untrusted.py` (`untrusted(text: str, source: str) -> str`: the `<data source="...">` block stage 2's context hooks already use, with any data tag inside the text neutralised; `agents/hooks/registry.py` calls it instead of its own builder, so the block has one source and the working rules' sentence on `<data>` blocks covers it), `training/constants.py` (`REQUEST_TAG = "mlops_agents.request_id"`, the tag the team's pipeline writes on its MLflow run; `EVALUATION_ARTIFACT = "mlops_agents/evaluation.json"` is stage 3's), `promotion/constants.py` (`REQUIRED_REPORT_FIELDS`, I37).
- `MonitoringSettings` (project settings, defaults in one place, stored in `monitoring_settings`, read and written only through contracts' accessor and `GET`/`PUT /api/projects/{id}/monitoring-settings`): `collection_interval_s=60` (at least 10), `grouping_window_minutes=60`, `reopen_window_hours=24`, `recent_change_window_hours=24`, `escalation_timeout_minutes=15`, `check_grace_minutes=30` (the grace of a check, which carries none), `second_deadline_minutes=60` (after the first deadline: a feed or job still silent then is past its second deadline), `parser_disable_after=3`, `evaluation_max_age_hours=72`, `circuit_breaker_fixes=3`, `circuit_breaker_window_days=30`, `timezone: TimeZoneName | None = None` (None = the server's). App settings (`app.db`, rows of the key-value `settings` table, keys in `notifications/contract.py`): `public_url: str | None`, `DiscordSettings`, `EmailSettings` (I28).
- `credentials/project_secrets.py` gains the one builder `hook_ref(project_id, hook_id)` (ref `project/<id>/hook/<hok>`, under `project_prefix`, so project removal deletes it) and `hook_prefix`. The app-level refs `DISCORD_WEBHOOK_REF` and `EMAIL_PASSWORD_REF` are constants in `notifications/contract.py`, written through `KeyStore` as the GitHub token is (`ProjectSecrets` takes only project refs). No other code writes a ref string.
- `PrObserver` (Protocol, `agents/pr_sync.py`): `async changed(project_id, pr: PrChange)`, with `PrChange(number: int, state: Literal["open","merged","closed"], merged_at: datetime | None, head_branch: str, agent: str, files: list[str])` (`files`: every path the PR changes, a rename's old and new path; stage 2's sync sees only agents' task PRs, so `agent` is always set; the team's own merges and deploys reach incidents through the `SyncObserver` below); `PrSyncObserver` gains `add_observer` (`sync_now` exists); `integrations/github/pulls.py` gains `PullRequest.merged_at` and `changed_files` and `list_pull_files(...) -> list[PullFile]` (`PullFile(filename, previous_filename: str | None)`, every page; the one read of a PR's files, which merging uses too); the fake GitHub serves them. Expectations, incidents, pins and merging subscribe.
- `HandoffObserver` (Protocol, `proposals/contract.py`, an I20 change, Decision #448): `async stored(project_id: str, handoff: HandoffView) -> None`, called by `Handoffs.store` after commit with `current()` (never a superseded version); `Handoffs` gains `add_observer(observer)` (contracts' only edit to `proposals/handoffs.py`). An observer that raises is logged and the others still run. Every observer also reconciles `Handoffs.current()` once per project at startup, since projects onboarded before stage 4 never fired it. Results (expectations), notifications (on-call) and hooks (registration) subscribe; nothing else reacts to a stored handoff.
- `QueryProviders` (`monitoring/query.py`, built once in `build_services` as `Services.query_providers`): `register(provider: QueryProvider) -> None` (a second provider for one `what` raises), `get(what) -> QueryProvider | None`; I33's query types beside it. Each plan registers the providers of its own tables there; the `MonitoringQuery` tool (incidents task 5) reads it.
- `cause_key(subject: str, model: str | None) -> str` in `monitoring/incidents/contract.py`, the only place a cause key is built; `ACTIVE_STATUSES` (`open`, `acknowledged`, `mitigated`, `reopened`) and `stages_of(subject: str, payload: HandoffPayload) -> list[str]` (the stages listing the subject exactly in `inputs` or `outputs`, decided point 6) beside it: grouping, severity and the Dashboard use these, never their own.
- Package `__init__.py` files: `monitoring/`, `monitoring/{results,expectations,scheduler,incidents,rules,hooks,verdicts}/`, `notifications/`, `training/`, `promotion/`, `stats/` by contracts task 1 (S5 gives no module layout: `training/` holds fixes, requests and policies; `promotion/` holds evaluation reading, reports and promotions; the pipeline-run parts of S5 §2 extend `monitoring/hooks/` as S5 says).
- Schema (task 2): one project migration `storage/migrations/project/<n>_stage4_5.sql` and `storage/migrations/app/0003_stage4_5.sql`, with `tables.py`. `<n>` is `0007` once stage 3 approvals-fixes' `0006` (#390) is on `main`, as planned (stage 3 knowledge #406 adds none); `load_migrations` refuses gaps, so contracts task 2 takes the next free number on `main` when its PR opens, and whichever of the two PRs merges second renumbers its own file. The schema-pin tests (`test_migrate.py`, `test_stage2_schema.py`, `test_stage3_schema.py`) gain the number; no other code or test names it. Contracts task 2 lists every table and column; a plan that needs another column opens a Decision. Stage 3's `evaluations` table is not recreated. In `app.db`: the price columns on `models` (I42); Discord, email and `public_url` are `settings` rows (no DDL).
- Dependency `croniter`.

### I22: results (`monitoring/results/contract.py`)

```python
ResultCategory = Literal["data_quality", "drift", "model_performance", "operational"]   # retypes CheckSpec.category; the parity test in contracts task 5 compares them
ResultOrigin = Literal["team", "agent", "tracker"]    # tracker: records the app derives itself from the tracking record (ci, mlflow); S4 names two origins, see decided point 1
SourceKind = Literal["file", "ci", "mlflow"]

class ResultRecord(BaseModel):          # what a check, a parser or a mapping produces
    check_id: str; origin: ResultOrigin; ran_at: datetime; fired: bool; value: float | None; threshold: float | None
    category: ResultCategory; subject: str; model: str | None; detail: dict[str, Any]
    source_ref: str; version: str | None; not_enough_data: bool = False   # version: the git commit of an agent's check; not_enough_data: a record, never a fire
class StoredResult(ResultRecord): id: str; source_id: str; source_key: str; seen_at: datetime   # res_...
class ResultQuery(BaseModel): ids: list[str] | None = None; check_id: str | None = None; model: str | None = None; origin: ResultOrigin | None = None; subject: str | None = None; fired: bool | None = None; since: datetime | None = None; until: datetime | None = None; latest: bool = False; limit: int = 50; offset: int = 0   # limit max 500; since and until inclusive, on ran_at
class SourceState(BaseModel): source_id: str; kind: SourceKind; cursor: dict[str, Any]; last_read: datetime | None; last_error: str | None; consecutive_failures: int
class CollectionReport(BaseModel): stored: int; by_source: dict[str, int]; failures: dict[str, str]

def source_id(kind: SourceKind, ref: str, connection: str | None = None) -> str: ...   # "file:<connection>:<pattern>", "ci:<workflow>", "mlflow:<metric>"; the only builder; `connection` is required for "file" (ValueError without it) and refused for the other kinds (2026-10-29, #529)
def parse_source_id(source_id: str) -> tuple[SourceKind, str, str | None]: ...   # (kind, ref, the connection name of a file source); its inverse

class ResultsObserver(Protocol):        # incidents, expectations, verdicts
    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None: ...   # new results, after commit; idempotent by result ID (a result is delivered again after a crash)

class Results(Protocol):                # results
    def add_observer(self, observer: ResultsObserver) -> None: ...
    async def ingest(self, project_id: str, source_id: str, items: Sequence[tuple[str, ResultRecord]], seen_at: datetime) -> list[StoredResult]: ...   # (source_key, record); idempotent by (source_id, source_key); returns only the new ones
    async def collect(self, project_id: str, now: datetime) -> CollectionReport: ...   # every source from its cursor, oldest first
    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]: ...
    async def latest_per_check(self, project_id: str, subject: str, model: str | None) -> list[StoredResult]: ...   # what "the latest results no longer fire" reads

class ParserMapping(BaseModel): format: Literal["jsonl", "json", "csv", "log"]; fields: dict[str, str]; pattern: str | None; fired_when: Condition; defaults: dict[str, Any] = {}   # fields: standard field -> JSON path, column or pattern group
class ParserCode(BaseModel): script: str; timeout_s: int = 30          # python, run in the sandbox over the read-only mount, no network
class ParserSpec(BaseModel): source_id: str; mapping: ParserMapping | None; code: ParserCode | None   # exactly one
class ParserVersion(BaseModel): source_id: str; version: int; spec: ParserSpec; proposal_id: str; approved: datetime; enabled: bool; consecutive_failures: int
class ParseOutcome(BaseModel): records: list[tuple[str, ResultRecord]]; errors: list[str]
class Parsers(Protocol):                # results
    async def store(self, project_id: str, spec: ParserSpec, proposal_id: str) -> ParserVersion: ...   # idempotent per proposal
    async def current(self, project_id: str, source_id: str) -> ParserVersion | None: ...
    async def parse(self, project_id: str, source_id: str, raw: str, origin_ref: str) -> ParseOutcome: ...   # validates against ResultRecord; a failure is a signal about the source, never a skip

class EvaluationCopies(Protocol):      # results task 4: the one write path of the evaluation copy into stage 3's `evaluations` table (I16 has none; I16 is untouched)
    async def store_copy(self, project_id: str, record: EvaluationRecord) -> bool: ...   # False when the tracker sync already stored (source, model, version)
```

- Cursors, per source: for a file, its size, modified time and byte offset (a file that shrank or changed before the offset is read again from the start); for `ci` and `mlflow`, the last run ID or time. `ci` maps a failed or cancelled run to a fired record (subject: the workflow); `mlflow` maps a metric point by the handoff's threshold. Both are origin `tracker`.
- Delivery: a stored result is marked delivered once every observer has returned; undelivered results are delivered again at startup and on each collection, so a crash between commit and observers loses nothing.
- Fired records become signals in incidents (a `ResultsObserver`). Results raises, through `Incidents.raise_signal`, only the signals about its own machinery and expectations: `silence` (expectations), `source_failure` (a source or line it cannot read) and `parser_failure`.

### I23: the one condition language (`monitoring/expr.py`, contracts task 4) and rules (`monitoring/rules/contract.py`)

```python
class Compare(BaseModel): op: Literal["lt", "le", "gt", "ge", "eq", "ne"]; field: str; value: float | str | bool
class AllOf(BaseModel): all_of: list["Condition"]
class AnyOf(BaseModel): any_of: list["Condition"]
class Not(BaseModel): not_: "Condition"
Condition = Compare | AllOf | AnyOf | Not           # depth at most 4, at most 32 nodes, validated
def evaluate(cond: Condition, row: Mapping[str, Any]) -> bool: ...   # pure, no eval; a missing field is False; `detail.<key>` addresses the detail dict

class RuleTerm(BaseModel): check_id: str; model: str | None; when: Condition; window_minutes: int; quantifier: Literal["latest", "any", "all"]; min_records: int = 1   # over stored results of that check inside the window
class RuleLimits(BaseModel): per_incident: int = 1; per_day: int = 1
class RuleSpec(BaseModel): name: str; why: str; model: str | None; terms: list[RuleTerm]; hook_id: str; hook_kind: Literal["rollback", "rerun", "fall_back"]; limits: RuleLimits   # every term must hold
class RuleView(BaseModel): id: str; spec: RuleSpec; enabled: bool; proposal_id: str; approved: datetime   # rul_...
class Rules(Protocol):                  # rules
    async def list(self, project_id: str, model: str | None = None) -> list[RuleView]: ...
    async def store(self, project_id: str, spec: RuleSpec, proposal_id: str) -> RuleView: ...   # idempotent per proposal
    async def set_enabled(self, project_id: str, rule_id: str, enabled: bool) -> RuleView: ...
```

- The parser's `fired_when` and a rule's terms use this one language and this one evaluator; no plan writes another.

### I24: scheduler and times (`monitoring/scheduler/`, `monitoring/jobs.py`)

```python
class JobKind(StrEnum):
    SYNC_TRACKING = "sync_tracking"; SYNC_PRS = "sync_prs"; COLLECT_RESULTS = "collect_results"; AFTER_SYNC = "after_sync"; CHECK_EXPECTATIONS = "check_expectations"
    FOLLOW_UPS = "follow_ups"; RETRY_NOTIFICATIONS = "retry_notifications"; ESCALATE = "escalate"; MORNING_REPORT = "morning_report"
class HeartbeatView(BaseModel): ok: Literal[True]                # the heartbeat route's whole answer
JOB_ORDER: Final[tuple[JobKind, ...]] = tuple(JobKind)         # the order a missed batch is run in (S4 §3); declaration order is the order
class JobDue(BaseModel): kind: JobKind; due: datetime; subject: str = ""          # subject: for example a person for the morning report; unique with kind and due

class SyncObserver(Protocol):           # the one seam for work that follows a tracking sync and a collection (I16 has no observer): results task 3 runs every registered observer in the AFTER_SYNC job
    async def synced(self, project_id: str, now: datetime) -> None: ...   # idempotent; each observer keeps its own cursor (for example the last deployment it saw)

class ScheduledJob(Protocol):           # each plan that owns a job implements one
    kind: JobKind
    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]: ...   # every due time in (after, now]
    async def run(self, project_id: str, job: JobDue) -> None: ...   # idempotent

class Scheduler(Protocol):              # scheduler; one loop in the app process, a job table per project, its own clock (Clock)
    def register(self, job: ScheduledJob) -> None: ...
    def add_sync_observer(self, observer: SyncObserver) -> None: ...
    async def run_due(self, project_id: str, now: datetime) -> None: ...   # runs every unrun due job in JOB_ORDER, then by due time
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    # a job that raises is recorded, not retried inside the loop, and the others still run; the scheduler never runs a check or a model

# times (results task 1)
def interval_dues(interval_s: int, after: datetime, now: datetime) -> list[datetime]: ...   # an interval job's dues: the latest point of a UTC grid in (after, now], or none; the one catch-up rule of interval jobs
def next_occurrences(cadence: Cadence, tz: str, after: datetime, until: datetime) -> list[datetime]: ...   # UTC instants; DST: a local time that does not exist is skipped, one that occurs twice fires once
def in_hours(hours: OnCallHours, tz: str, at: datetime) -> bool: ...                # an end before the start crosses midnight
def local_day_start(tz: str, at: datetime) -> datetime: ...
```

- Each job's owner registers it in `build_services` with `Scheduler.register`: results `SYNC_TRACKING`, `SYNC_PRS`, `AFTER_SYNC` (task 3), `COLLECT_RESULTS` (task 4), `CHECK_EXPECTATIONS` (task 5); incidents `FOLLOW_UPS`; notifications `RETRY_NOTIFICATIONS`, `ESCALATE`, `MORNING_REPORT`. Interval jobs give one due per gap when catching up (they read from cursors); the morning report gives the latest missed one per person. Sync observers: verdicts (confirmation by labels), training (linking by the tag; the alias-history check, the pin check on the default branch), promotion (the post-deploy trigger on a moved production alias; `team_gate` results), incidents (the team's merges and deploys as suspects, from `ci_runs` and `deployments`).
- The scheduler and every stage 4 to 6 service share one `Clock`: `create_app` and `build_services` take `clock: Clock | None` (results task 1).
- The heartbeat: `GET /api/health/heartbeat` returns `HeartbeatView` (`{"ok": true}`) and nothing else. The Dashboard's `heartbeat_reachable` is `settings.mode is RunMode.SERVER` (in local mode the heartbeat cannot be reached from outside).

### I25: expectations and checks (`monitoring/expectations/contract.py`)

```python
class ExpectationKey(BaseModel): kind: Literal["feed", "job", "check", "training_request"]; ref: str; model: str | None = None
class ExpectationSpec(BaseModel):       # exactly one of cadence and due_at
    key: ExpectationKey; category: ResultCategory; cadence: Cadence | None; due_at: datetime | None; timezone: str
    grace_minutes: int | None; second_deadline: bool; activates_on_pr: int | None; source: Literal["handoff", "check", "training"]
    # grace None: MonitoringSettings.check_grace_minutes at evaluation; second_deadline: a second signal past grace + MonitoringSettings.second_deadline_minutes (feeds and jobs)
SECOND_DEADLINE_KEY: Final = "second_deadline"   # the silence signal's detail key, true past the second deadline; severity reads it
class ExpectationState(BaseModel): key: ExpectationKey; category: ResultCategory; active: bool; next_deadline: datetime | None; last_seen: datetime | None; status: Literal["ok", "due", "late", "silent", "suspended", "pending_pr"]; since: datetime | None
class CheckView(BaseModel): id: str; check_id: str; category: ResultCategory; cadence: Cadence; timezone: str; results_location: FileResults | MlflowResults; pr_number: int; agent: str; version: str | None; status: Literal["pending_pr", "active", "retired"]; registered: datetime   # chk_...

class Maintenance(Protocol):            # incidents task 7; until then contracts' default `NeverSuspended` (decided point 19); asked by expectations, incidents and results
    async def suspended(self, project_id: str, scope: ExpectationKey | str, at: datetime) -> bool: ...   # a feed or job key, or a connection name
class Expectations(Protocol):           # expectations
    async def register(self, project_id: str, spec: ExpectationSpec) -> None: ...   # idempotent by key; replaces the spec
    async def retire(self, project_id: str, key: ExpectationKey) -> None: ...
    async def note_seen(self, project_id: str, key: ExpectationKey, ran_at: datetime) -> None: ...
    async def activate_for_pr(self, project_id: str, pr_number: int) -> list[ExpectationKey]: ...
    async def evaluate(self, project_id: str, now: datetime) -> list[str]: ...        # silence signal IDs raised, one per miss
    async def states(self, project_id: str) -> list[ExpectationState]: ...
```

- Expectations are seeded from `Operations.feeds` and `Operations.checks` of the current handoff (through `HandoffObserver` and the startup reconcile), with a `Feed`'s own cadence, grace and time zone.
- `Maintenance` is asked by whoever raises a signal it could suspend: expectations (by key), incidents' results observer (by the record's connection, `parse_source_id`), results' source failures (by connection). A suspended scope is recorded and raises nothing.

### I26: signals and incidents (`monitoring/incidents/contract.py`)

```python
Severity = Literal["critical", "high", "medium", "low"]
IncidentStatus = Literal["open", "acknowledged", "mitigated", "resolved", "closed", "reopened", "merged"]   # reopened behaves as open; merged: absorbed by `merged_into`, never counted by Stats
SignalKind = Literal["fired_result", "silence", "parser_failure", "source_failure", "notification_failure"]   # a failed CI run is a fired `ci` record (fired_result)

class SignalDraft(BaseModel):
    kind: SignalKind; category: ResultCategory; subject: str; model: str | None; ran_at: datetime
    detail: dict[str, Any]; result_id: str | None; source_ref: str | None; expectation: ExpectationKey | None
    cleared: bool = False             # read after its condition had already cleared (catching up): opens an incident resolved while offline, waking no one
class SignalView(SignalDraft): id: str; incident_id: str; seen_at: datetime; cause_key: str          # sig_...
class IncidentView(BaseModel):
    id: str; category: ResultCategory; subtype: str | None; severity: Severity; status: IncidentStatus; cause: str | None; cause_key: str; model: str | None
    signal_ids: list[str]; prs: list[int]; issues: list[int]; held: bool; merged_into: str | None
    started_at: datetime; detected_at: datetime; acknowledged_at: datetime | None; mitigated_at: datetime | None; resolved_at: datetime | None; closed_at: datetime | None
    snoozed_until: datetime | None; resolved_offline: bool; reopened_count: int; verdict: "VerdictView | None"            # inc_...; verdict through `Verdicts.get` once stats task 1 binds it
class HoldScope(BaseModel): tables: list[str]; start: datetime | None; end: datetime | None   # the data held or quarantined; empty tables: the whole model
class IncidentPatch(BaseModel): cause: str | None = None; category: ResultCategory | None = None; subtype: str | None = None; status: Literal["mitigated", "resolved"] | None = None; note: str | None = None
class SignalOutcome(BaseModel): signal_id: str; incident_id: str; change: Literal["opened", "attached", "reopened", "quiet"]; severity: Severity
class IncidentChange(BaseModel): incident_id: str; kind: Literal["opened", "severity_raised", "severity_lowered", "reopened", "status", "cause", "merged", "split", "hold"]; before: Severity | None; after: Severity | None

class IncidentObserver(Protocol):       # severity (the reactor), notifications' escalation, dashboard
    async def changed(self, project_id: str, change: IncidentChange) -> None: ...   # awaited before raise_signal returns

class Incidents(Protocol):              # incidents
    def add_observer(self, observer: IncidentObserver) -> None: ...
    async def raise_signal(self, project_id: str, draft: SignalDraft, seen_at: datetime) -> SignalOutcome: ...   # attach, open or reopen; quiet period: an attach that does not raise severity notifies no one
    async def get(self, project_id: str, incident_id: str) -> IncidentView | None: ...
    async def list(self, project_id: str, status: list[IncidentStatus] | None = None, limit: int = 50, offset: int = 0) -> Page[IncidentView]: ...   # ranked: severity, then newest detected_at
    async def signals(self, project_id: str, incident_id: str) -> list[SignalView]: ...
    async def holds(self, project_id: str, model: str | None) -> list[HoldScope]: ...   # holds on incidents not resolved or closed, of that model or of none; what the fixes guard reads
    async def acknowledge(self, project_id: str, incident_id: str, by: str) -> IncidentView: ...      # person only: the route, never a tool
    async def snooze(self, project_id: str, incident_id: str, until: datetime, by: str) -> IncidentView: ...
    async def close(self, project_id: str, incident_id: str, by: str) -> IncidentView: ...
    async def update(self, project_id: str, incident_id: str, agent: str, patch: IncidentPatch) -> IncidentView: ...   # `resolved` only when `Results.latest_per_check` for the cause key no longer fires (checked here)
    async def merge(self, project_id: str, ids: list[str], agent: str) -> IncidentView: ...
    async def split(self, project_id: str, incident_id: str, signal_ids: list[str], agent: str) -> IncidentView: ...
    async def record_hold(self, project_id: str, incident_id: str, scope: HoldScope, reason: str, by: str) -> IncidentView: ...   # the scope is what the fixes guard tests a training window against
    async def set_severity(self, project_id: str, incident_id: str, severity: Severity, reason: str, by: str) -> IncidentView: ...   # `by`: severity (code) or Alert Prioritization; the reactor re-routes
```

- Grouping is S4 §5: the same cause key within `grouping_window_minutes`, or a subject downstream of the incident's subject in the pipeline graph (`edges` and `stages_of`; a subject that matches no stage groups by cause key only, decided point 6).
- Signals from `team`-origin records are never raised (S4 §10: the baseline never opens incidents); `tracker`-origin and `agent`-origin fired records, silences and failures of the app's own machinery are.
- A `reopened` incident is the same ID. `merge` keeps the oldest incident; the others become `merged` with `merged_into` set (only a person closes, S4 §5).
- The timeline is the shared event list above, read with `EventLog.page(subject_id=...)`; nothing is stored twice.
- `IncidentsService.bind_verdicts(verdicts: Verdicts)` (stats task 1 calls it in `build_services`, as `bind` is used elsewhere): until then `verdict` is `None`.

### I27: severity, routing and waking an agent (types in `monitoring/incidents/contract.py`; implemented in `monitoring/incidents/severity.py`, `monitoring/incidents/routing.py`, `monitoring/waker.py`)

```python
class SeverityDecision(BaseModel): severity: Severity; reasons: list[str]
class SeverityPolicy(Protocol):         # incidents task 4; contracts' default (`MediumSeverity`) returns medium
    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision: ...   # deterministic code, never a model: harm now, silence of a feed or job production depends on (second deadline: critical), cost of error, scope, confidence
class Delivery(BaseModel): person: str; when: Literal["now", "morning_report"]; escalation_step: int   # the Notifier sends to every enabled contact of the person
class RoutePlan(BaseModel): deliveries: list[Delivery]; inbox: bool; open_issue: bool
class Router(Protocol):                 # incidents task 4
    async def plan(self, project_id: str, incident: IncidentView, now: datetime) -> RoutePlan: ...   # critical: on-call at once at any hour, escalation after `escalation_timeout_minutes`; high: now inside the person's hours, else the morning report; medium, low: Inbox and the morning report; above the issue threshold: open_issue

WakeLabel = Literal["incident", "fix_request", "post_deploy", "follow_up"]
class AgentWaker(Protocol):             # incidents task 2 (early PR); no other code posts a system message to a session
    async def wake(self, project_id: str, agent: str, label: WakeLabel, subject_id: str, text: str, key: str) -> None: ...   # a system message in the agent's session, labeled; idempotent by (agent, label, subject_id, key); outside stage 2's hop and budget limits
def diagnoser(category: ResultCategory, subtype: str | None) -> str: ...   # data_quality and drift: data-quality; model_performance: model-performance; operational: retraining for subtype failed_training, deployment for bad_deploy and failed_scoring, else orchestrator
```

- Paging never waits for a model: the reactor (an `IncidentObserver`, incidents task 4) plans the route and sends every `now` delivery, on open, reopen, a severity rise and a hold, and only then wakes the diagnosing agent. The message says the diagnosis follows. Notifications task 4 sends only the escalation steps after step 0.
- `AgentWaker.wake`'s `subject_id` is the incident for `incident`, `fix_request` and `follow_up` (the fix or follow-up ID is in `key`), and the promotion or the model for `post_deploy`; a wake over `MAX_WAKES_PER_HOUR` records `incident.wake_skipped` with that `subject_id`.
- Alert Prioritization (`chat: false`) is woken once per new or changed incident; it merges (`IncidentMerge`) and may move severity one level with `AdjustSeverity`. A system wake is the one message a `chat: false` agent accepts (incidents task 6, in `agents/runtime/sessions.py`).

### I28: notifications and the morning report (`notifications/contract.py`)

```python
class DiscordSettings(BaseModel): enabled: bool; webhook_set: bool                 # the URL is in the keystore (DISCORD_WEBHOOK_REF)
class EmailSettings(BaseModel): enabled: bool; host: str; port: int; user: str; sender: str; starttls: bool; password_set: bool   # the password is in the keystore (EMAIL_PASSWORD_REF)
class Message(BaseModel): title: str; severity: Severity; cause: str | None; done: list[str]; waiting: list[str]; lines: list[str] = []; link: str | None; local_mode_note: bool; urgent: bool; diagnosis_pending: bool   # lines: the report's incident list
class ChannelError(Exception): ...                                              # user-facing, scrubbed
class Channel(Protocol): name: Literal["discord", "email"]; async def send(self, target: str, message: Message) -> None: ...   # raises ChannelError
class OnCallPerson(OnCall): id: str                                              # a slug of the name, unique in the project
class OnCallPeople(Protocol):           # notifications task 2
    async def list(self, project_id: str) -> list[OnCallPerson]: ...   # by escalation_order, then id
    async def replace(self, project_id: str, people: list[OnCall]) -> list[OnCallPerson]: ...
NotificationKind = Literal["incident", "escalation", "report", "promotion"]   # promotion: a team gate's result
class Notifier(Protocol):               # notifications task 1
    async def notify(self, project_id: str, person: OnCallPerson, message: Message, *, kind: NotificationKind, incident_id: str | None) -> Literal["sent", "retrying", "failed"]: ...   # every enabled contact; retried by the RETRY_NOTIFICATIONS job and logged in `notification_log`; a channel failing repeatedly raises a `notification_failure` signal (task 4)
class ReportAction(BaseModel): incident_id: str | None; action: str; why: str; basis: Literal["rule", "policy", "approval", "tier1", "bypass"]; basis_ref: str | None
class MorningReportBody(BaseModel): person: str; since: datetime; incidents: list[str]; actions: list[ReportAction]; waiting: list[str]; suspended: list[ExpectationKey]; resolved_offline: list[str]; wakes_skipped: list[str]
class MorningReports(Protocol): async def build(self, project_id: str, person_id: str, since: datetime) -> MorningReportBody: ...
DISCORD_WEBHOOK_REF: Final; EMAIL_PASSWORD_REF: Final; PUBLIC_URL_KEY: Final; DISCORD_SETTINGS_KEY: Final; EMAIL_SETTINGS_KEY: Final   # keystore refs and app `settings` keys
```

- Links use `public_url` (incident page path `/projects/{project_id}/incidents/{incident_id}`, ui's route); without it the message carries the full summary, and in local mode it sets `local_mode_note`. "Waking" is a Discord mention and an email marked urgent. `IntegrationSettings` (notifications task 1) is the one reader and writer of the three app settings.

### I29: action hooks (`monitoring/hooks/contract.py`)

```python
HookStatus = Literal["active", "suspended", "disabled"]
class PinnedFile(BaseModel): path: str; sha256: str
class HookView(BaseModel):              # hok_...; spec.kind is the hook kind (CommandSpec.kind)
    id: str; name: str; model: str | None; spec: CommandSpec; status: HookStatus; suspended_reason: str | None; credential_set: bool
    pins: list[PinnedFile]; approved_commit: str | None; deploys_verified: bool | None; source: Literal["handoff", "proposal"]; created: datetime   # deploys_verified None until a run was checked
class HookCall(BaseModel): hook_id: str; reason: str; incident_id: str | None = None; fix_id: str | None = None; params: dict[str, str] = {}; rule_id: str | None = None; policy_id: str | None = None
class HookResult(BaseModel): ok: bool; detail: str; ci_run_url: str | None; request_id: str | None   # detail scrubbed and capped
class HookRefused(Exception): ...                                               # reason is user-facing
class HookRunView(BaseModel): id: str; hook_id: str; kind: str; model: str | None; incident_id: str | None; fix_id: str | None; rule_id: str | None; policy_id: str | None; reason: str; basis: Literal["rule", "policy", "approval", "none"]; basis_ref: str | None; evidence: list[str]; ok: bool | None; detail: str | None; request_id: str | None; started: datetime; ended: datetime | None   # kind: the hook's CommandSpec.kind; ok None while running

class HookRunner(Protocol): type: Literal["command", "ci_dispatch"]; async def run(self, project_id: str, hook: HookView, params: Mapping[str, str], authority: Authority) -> HookResult: ...
class HookLifecycle(Protocol):          # seams pipeline-runs and training use
    async def before(self, project_id: str, hook: HookView, call: HookCall) -> HookCall: ...   # may fill params or raise HookRefused
    async def after(self, project_id: str, hook: HookView, call: HookCall, result: HookResult) -> None: ...
class HookPinner(Protocol):             # training task 7
    async def pins(self, project_id: str, hook: HookView) -> tuple[str, list[PinnedFile]]: ...   # (the default branch's commit, the hashes there)
class Hooks(Protocol):                  # hooks
    def add_runner(self, runner: HookRunner) -> None: ...
    def add_lifecycle(self, lifecycle: HookLifecycle) -> None: ...
    def set_pinner(self, pinner: HookPinner) -> None: ...   # until set, hooks carry no pins
    async def list(self, project_id: str, model: str | None = None) -> list[HookView]: ...
    async def get(self, project_id: str, hook_id: str) -> HookView | None: ...
    async def register(self, project_id: str, spec: CommandSpec, name: str, model: str | None, source: Literal["handoff", "proposal"]) -> HookView: ...   # idempotent by (model, name); pins through the pinner
    async def set_enabled(self, project_id: str, hook_id: str, enabled: bool) -> HookView: ...   # never clears a suspension
    async def suspend(self, project_id: str, hook_id: str, reason: str) -> HookView: ...
    async def set_deploys_verified(self, project_id: str, hook_id: str, verified: bool) -> HookView: ...   # the alias check's verdict
    async def approve(self, project_id: str, hook_id: str, by: str) -> HookView: ...   # person only: a route (training task 7), never a tool; re-pins at the current default branch, clears the suspension
    async def run(self, project_id: str, call: HookCall, authority: Authority) -> HookResult: ...   # after the gate: lifecycle.before, the runner, lifecycle.after; records `hook.ran` or `hook.failed` on the incident; never retries; refuses only a `disabled` hook
    async def runs(self, project_id: str, *, kinds: Sequence[str] | None = None, since: datetime | None = None, limit: int = 200) -> list[HookRunView]: ...   # newest first; the one read of hook runs (morning report, Deployments, Stats)
```

- The handoff's `commands` (project level and per model) become hooks through `HandoffObserver` and the startup reconcile (`source: "handoff"`), named by their kind (`rollback`, then `rollback-2`, ... in handoff order); a `handoff` hook missing from a newer handoff is `disabled`. The `hooks` proposal adds or changes later ones (decided point 7).
- A hook with a credential reads it with `hook_ref` and the credential reaches only that hook's own container run (the command runner, with network: it acts on the team's systems), never an agent's Bash; the command reads it from `MLOPS_AGENTS_HOOK_CREDENTIAL`.
- A `suspended` hook still runs, as a deploy action: `GateFacts` reports it as deploying, so the gate asks.
- `Hold(incident_id, scope, reason)` records the hold on the incident and, when the model (else the project) has one active hold hook, runs it; with none it is a notification only (the reactor sends the hold change's deliveries), and the first audit reports the missing command as a gap.

### I30: the gate's second input (`agents/approvals/authority.py`, `agents/policy/actions.py`; contracts task 3)

```python
ActionKind = Literal["train", "evaluate", "deploy", "rollback", "rerun", "hold", "fall_back", "merge_pr"]   # CommandSpec.kind plus merge_pr; parity-tested
class ActionCall(BaseModel):            # built by a tool's `action_call(args, ctx)`; None for every other tool
    project_id: str; agent: str; kind: ActionKind; hook_id: str | None; model: str | None; incident_id: str | None
    rule_id: str | None; policy_id: str | None
    deploys: bool | None; has_production_signal: bool | None; team_gate_authority: bool | None   # filled by the gate from `GateFacts`, never from the agent
class Authority(BaseModel): basis: Literal["rule", "policy", "approval", "none"]; ref: str | None; reason: str; evidence: list[str]   # evidence: result IDs
class ActionAuthority(Protocol):        # rules and training implement one each; the contracts default says basis "none"
    async def authorize(self, call: ActionCall) -> Authority: ...      # evaluates in code against stored records and limits; never trusts the agent's claim
class GateFacts(Protocol): async def facts(self, project_id: str, hook_id: str | None) -> tuple[bool | None, bool | None, bool | None]: ...   # (deploys: the spec's, or true while suspended; has production signal; team gate is authority); training task 7; contracts default `NoGateFacts`: all None
class AuthorityRegistry:
    def register(self, citation: Literal["rule", "policy"], authority: ActionAuthority) -> None: ...   # twice for one citation raises
    def set_facts(self, facts: GateFacts) -> None: ...   # training task 7 replaces the default; the gate reads authorities and facts from here
def decide_action(level: PermissionLevel, call: ActionCall, authority: Authority) -> PolicyDecision: ...   # agents/policy/actions.py; a second data table, the roadmap's `deploy` table in code
PROPOSE_BYPASS_OVERRIDES: Final[Mapping[str, PolicyDecision]] = {"promotion": "allow"}   # the one `propose` kind that skips its approval at bypass, after the code checks of S5 §4; every other kind asks at every level
# GateDecision gains `authority: Authority | None` and ToolContext gains `authority: Authority | None`: the Authority the call was allowed on (basis `approval`, ref the approval ID, after an approved ask); `Hooks.run` takes it from there
```

The gate (contracts) overwrites the call's three facts from `GateFacts`, consults the registered authority for the citation the kind takes (`rule_id` for `rollback`, `rerun`, `fall_back`; `policy_id` for `train`, `evaluate`; anything else gives basis `none`), then `decide_action`. A `None` fact never selects the more permissive row: `train` and `evaluate` whose `deploys` is not `False`, and any other kind but `deploy` and `merge_pr` whose `deploys` is `True` (a suspended hook), take the "run that deploys" rows. Cells (the roadmap's table, written out; the cells marked "?" were confirmed by decided point 2):

| Action | Basis | suggest | ask | autonomous | bypass |
|---|---|---|---|---|---|
| promotion (`deploy`), `merge_pr` | any | deny | ask | ask | allow |
| `train`, `evaluate` | policy | deny ? | allow | allow | allow |
| `train`, `evaluate` | none | deny ? | ask | ask | ask ? |
| a run that deploys, team gate is the authority, production signal known | policy | deny ? | allow | allow | allow |
| a run that deploys, otherwise | none | deny ? | ask | ask | ask ? |
| `rollback`, `rerun`, `fall_back` | rule | deny | allow | allow | allow |
| `rollback`, `rerun`, `fall_back` | none | deny | ask | ask | allow |
| `hold` | any | ask ? (the one click) | allow | allow | allow |

An approved `promotion` proposal's deploy-hook run is its apply, with basis `approval`; it is not gated again. The `propose` class still asks at every level for every kind except `promotion` at `bypass`.

### I31: verdicts and the baseline (`monitoring/verdicts/contract.py`)

```python
VerdictValue = Literal["real_issue", "not_an_issue", "wrong_type"]
class VerdictDraft(BaseModel): value: VerdictValue; right_category: ResultCategory | None; right_subtype: str | None; note: str | None   # right_* only for wrong_type
class VerdictView(BaseModel): incident_id: str; value: VerdictValue; right_category: ResultCategory | None; right_subtype: str | None; source: Literal["user", "labels"]; check_versions: dict[str, str]; note: str | None; created: datetime   # the versions of the checks the verdict judged
class MissReport(BaseModel): description: str; category: ResultCategory | None; occurred_at: datetime; reported_at: datetime
class BaselineLink(BaseModel): incident_id: str; team_check_id: str; first_fired_at: datetime; seen_at: datetime; lead_seconds: float   # negative: the team's check fired first
SilentVerdict = Literal["real_issue", "not_an_issue"]
class SilentTeamAlert(BaseModel): result_id: str; check_id: str; subject: str; ran_at: datetime; verdict: SilentVerdict | None   # a team check fired and no agent incident covers its cause key; listed for verdicts
class Verdicts(Protocol):               # verdicts (stats task 1)
    async def record(self, project_id: str, incident_id: str, draft: VerdictDraft, by: str) -> VerdictView: ...   # a person; a verdict on a `resolved` incident closes it (the person's act); a `labels` verdict never closes
    async def get(self, project_id: str, incident_id: str) -> VerdictView | None: ...   # what `IncidentView.verdict` shows
    async def confirm_by_labels(self, project_id: str) -> list[VerdictView]: ...                        # model performance incidents, past the minimum sample, from Model Performance's label-based results
    async def report_miss(self, project_id: str, miss: MissReport) -> None: ...
    async def list(self, project_id: str, since: datetime | None = None) -> list[VerdictView]: ...
class Baseline(Protocol):               # stats task 2; never notifies and never opens an incident
    async def available(self, project_id: str) -> bool: ...   # true once a `team`-origin result is stored; the Dashboard and Stats read it
    async def links(self, project_id: str, incident_id: str) -> list[BaselineLink]: ...
    async def silent_alerts(self, project_id: str) -> list[SilentTeamAlert]: ...
    async def judge_silent(self, project_id: str, result_id: str, value: SilentVerdict, by: str) -> SilentTeamAlert: ...   # a person's verdict on a silent alert
```

- `VerdictDraft` validates that `right_category` is set exactly for `wrong_type` and `right_subtype` only with it.

### I32: proposal payloads of stages 4 and 5 (`proposals/` kinds' payload models in each owner's contract file, kind names and keys by contracts task 4)

```python
PARSER_KIND = "parser"; RULE_KIND = "rule"; HOOKS_KIND = "hooks"; MAINTENANCE_KIND = "maintenance"; TRAINING_POLICY_KIND = "training_policy"; PROMOTION_KIND = "promotion"
class ParserPayload(BaseModel): spec: ParserSpec; sample_input: str; sample_records: list[ResultRecord]          # key: the source ID
class RulePayload(BaseModel): spec: RuleSpec                                                                    # key: model and rule name
class HookProposal(BaseModel): name: str; model: str | None; spec: CommandSpec; why: str
class HooksPayload(BaseModel): hooks: list[HookProposal]                                                        # key: the project
class WindowSpec(BaseModel): start: datetime; end: datetime; scope: list[str]; reason: str                      # scope: feed or job names, connection names
class MaintenancePayload(BaseModel): window: WindowSpec                                                         # key: reason and start
class TrainingPolicyPayload(BaseModel): ...                                                                     # I35; key: the model
class PromotionPayload(BaseModel): ...                                                                          # I37; key: the model
```

- Each kind implements `ProposalKind` (I19): preview, warnings, `credentials_needed` (none of the six needs a slot: a hook's credential is set on its route), idempotent `apply`; registered in `build_services` by the plan that owns it, with its card by that plan. The first five ask at every level; `promotion` per the override above, and an allowed propose is applied at once by the proposals service (contracts task 4). Proposals supersede by kind and key, as stage 3 decided. Keys: parser, the source ID; rule, model and rule name; hooks, constant per project; maintenance, reason and start; training policy and promotion, the model.
- `ProposalCheck` (Protocol, `proposals/contract.py`, runtime-checkable, an I19 addition): `async check(project_id, payload) -> list[str]`, code refusals at propose time; a kind may also implement it (promotion does), and `Propose` refuses before the gate when it returns any. The stage 3 kinds are unchanged.
- `Propose` takes every registered kind: `ProposeArgs.payload` is a plain object validated by the kind's `payload_model` (contracts task 4).
- `rule`, `training_policy` and any `hooks` entry whose spec has `deploys: true` show that fact in their warnings.

### I33: tools and the monitoring query (`agents/tools/names.py` by contracts; each tool by the named plan)

| Tool | Class | Plan | Notes |
|---|---|---|---|
| `RegisterCheck(check_id, category, cadence, timezone, results_location, pr_number)` | read | results | S4 §4 |
| `IncidentUpdate(incident_id, cause?, category?, subtype?, status?, note?)` | read | incidents | S4 §5; status only `mitigated` or `resolved` |
| `IncidentMerge(ids)`, `IncidentSplit(id, signal_ids)` | read | incidents | S4 §5 |
| `IncidentFollowUp(incident_id, after_days?, after_labels?, note)` | read | incidents | not in the spec: S4 §3 and §6 need a way to register "re-check after N labels or days" (decided point 3) |
| `AdjustSeverity(incident_id, direction, reason)` | read | incidents | Alert Prioritization only; not in the spec (decided point 3) |
| `MonitoringQuery(what, ...)` | read | incidents | below |
| `RunHook(hook_id, reason, incident_id?, fix_id?, params?, rule_id?, policy_id?)` | deploy | hooks | `action_call` gives the hook's kind and the cited rule or policy |
| `Hold(incident_id, scope, reason)` | deploy | hooks | not in the spec (decided point 3); gated as kind `hold` |
| `RequestFix(incident_id, evidence)` | read | training | S5 §1 |
| `RecordFixChoice(fix_id, choice, why, why_not)` | read | training | Retraining only; not in the spec |
| `SaveReport(draft)` | read | promotion | not in the spec: the report is stored before the `promotion` proposal cites it |
| `MergePullRequest(pr_number)` | deploy | promotion | S5 |

```python
QueryWhat = Literal["results", "signals", "incidents", "expectations", "checks", "rules", "hooks", "windows", "verdicts", "fixes", "training_requests", "policies", "reports", "promotions"]
class MonitoringQuery(BaseModel): what: QueryWhat; id: str | None = None; model: str | None = None; subject: str | None = None; incident_id: str | None = None; status: str | None = None; since: datetime | None = None; limit: int = 50; offset: int = 0   # limit max 200
class QueryProvider(Protocol): what: QueryWhat; async def query(self, project_id: str, q: MonitoringQuery) -> Page[Any]: ...   # each plan registers the providers of its own tables on QueryProviders
```

- These types and `QueryProviders` are in `monitoring/query.py` (contracts task 1). Providers: results (`results`, `expectations` task 5; `checks` task 7), incidents (`signals`, `incidents` task 5; `windows` task 7), hooks (`hooks` task 3; `rules` task 6), stats (`verdicts` task 1), training (`fixes`, `training_requests` task 5; `policies` task 8), promotion (`reports`, `promotions` task 6).
- The issue threshold is stage 2's setting, read through its accessor and not stored again.
- The `MonitoringQuery` tool (incidents task 5) dispatches to the registered providers and renders rows as JSON inside one `untrusted()` block; a row's free text (a result's `detail`, a signal's detail, a note) is never part of an instruction. Agents need it because the sandbox cannot read `project.db`.

### I34: fix requests (`training/contract.py`)

```python
FixChoice = Literal["re_threshold", "recalibrate", "retrain", "decline"]
FixStatus = Literal["requested", "chosen", "running", "done", "declined", "escalated"]
class FixRequestView(BaseModel): id: str; incident_id: str; model: str; requested_by: str; evidence: str; choice: FixChoice | None; why: str | None; why_not: dict[str, str]; status: FixStatus; outcome: Literal["pending", "cleared", "not_cleared"]; created: datetime   # fix_...
class GuardRefusal(BaseModel): code: Literal["data_held", "open_data_quality_incident", "labels_immature", "policy_limit", "circuit_open"]; detail: str
class GuardResult(BaseModel): ok: bool; refusals: list[GuardRefusal]
class TrainingWindow(BaseModel): start: datetime; end: datetime
class CircuitState(BaseModel): open: bool; failed_fixes: int; window_days: int
class Fixes(Protocol):                  # fixes
    async def request(self, project_id: str, incident_id: str, agent: str, evidence: str) -> FixRequestView: ...   # wakes Retraining, label fix_request
    async def choose(self, project_id: str, fix_id: str, agent: str, choice: FixChoice, why: str, why_not: dict[str, str]) -> FixRequestView: ...   # only Retraining; `decline` sends the incident back to observe
    async def guards(self, project_id: str, model: str, window: TrainingWindow, policy_id: str | None) -> GuardResult: ...   # the one place the guards live: held data, an open data_quality incident whose cause key covers the window's tables and dates, labels younger than the profile's `label_maturity_days`, the policy's limits (via the policy ActionAuthority), the circuit
    async def circuit(self, project_id: str, model: str) -> CircuitState: ...
    async def get(self, project_id: str, fix_id: str) -> FixRequestView | None: ...
```

### I35: training requests and policies (`training/contract.py`)

```python
class WindowRule(BaseModel): kind: Literal["trailing_days", "since_last_run", "fixed"]; days: int | None; start: datetime | None; end: datetime | None
class TrainingPolicyPayload(BaseModel): model: str; train_hook_id: str; evaluate_hook_id: str | None; window: WindowRule; min_interval_hours: int; max_runs_per_week: int; run_time_limit_minutes: int   # the limit the team's workflow enforces; the app states it, cannot enforce it
class TrainingPolicyView(BaseModel): id: str; payload: TrainingPolicyPayload; status: Literal["active", "suspended", "disabled"]; suspended_reason: str | None; proposal_id: str; approved: datetime   # pol_...
class TrainingRequestView(BaseModel): id: str; fix_id: str | None; model: str; hook_id: str; policy_id: str | None; window: TrainingWindow | None; mode: Literal["train", "evaluate"]; status: Literal["started", "linked", "failed", "silent"]; run_key: str | None; ci_run_url: str | None; started: datetime   # trq_...; the ID is `{request_id}`
class Training(Protocol):               # training
    async def policies(self, project_id: str, model: str | None = None) -> list[TrainingPolicyView]: ...
    async def request(self, project_id: str, request_id: str) -> TrainingRequestView | None: ...
    async def requests(self, project_id: str, model: str, limit: int = 50) -> list[TrainingRequestView]: ...
```

- Training registers a `HookLifecycle`: for every hook kind it resolves the declared placeholders (`PlaceholderResolver`; `{candidate_version}`, `{threshold}`, `{window_start}`, `{window_end}` come from `HookCall.params`); for `train` and `evaluate` it also takes the window (from the call or the policy; none: refused), runs `Fixes.guards`, creates the `trq` (`{request_id}`, `{mode}`), and raises `HookRefused` with the refusals. When a request links, Retraining is woken (`fix_request`, key `linked:<trq>`) to read the evaluation and write the report. After a started request it registers a `training_request` expectation (a tagged run by a deadline derived from the policy's run-time limit; silence raises a signal); a run whose `REQUEST_TAG` tag is seen in the tracking record links the request. A policy for a hook that deploys requires an approved rollback rule for that model, checked at apply and at every run.
- The policy `ActionAuthority` counts every run of the pipeline, the team's own included, from `Tracking.query(what="ci_runs")` for the interval and runs per week.

### I36: running the team's pipeline (`monitoring/hooks/`, pipeline-runs)

```python
class RunContext(BaseModel): request_id: str | None; model: str; champion_version: str | None; window: TrainingWindow | None; mode: Literal["train", "evaluate"] | None; candidate_version: str | None; threshold: float | None
class MissingPlaceholder(HookRefused): placeholder: str
class PlaceholderResolver(Protocol):    # training task 3 (early PR)
    async def resolve(self, hook: HookView, ctx: RunContext) -> dict[str, str]: ...   # resolved at the moment of the run; `champion_version` read from the tracker then; a placeholder the hook declares and the context lacks raises MissingPlaceholder before running
class DispatchRef(BaseModel): workflow: str; run_url: str | None
class CiDispatcher(Protocol): async def dispatch(self, project_id: str, workflow: str, inputs: dict[str, str]) -> DispatchRef: ...   # always the default branch, through the app's GitHub client
class PinCheck(BaseModel): ok: bool; changed: list[str]
class AliasCheck(BaseModel): moved: bool; detail: str
class HookVerifier(Protocol):
    async def verify_pins(self, project_id: str, hook_id: str) -> PinCheck: ...        # hashes of the workflow file and the declared entry files on the default branch against `HookView.pins`
    async def check_run(self, project_id: str, hook_id: str, request_id: str, window: TrainingWindow) -> AliasCheck: ...   # the tracker's alias history over the run's window
```

- A `deploys: false` run that moved a production alias to the version that run registered suspends the hook and every policy using it, raises a high-severity incident, and treats the hook's runs as deploy actions until the person approves again (`Hooks.approve`); a run checked without such a move sets `deploys_verified`. A changed pin does the same suspension. A hook whose model has no production signal never runs under a policy: `GateFacts` reports `has_production_signal = false`, so the gate asks.
- A changed pinned file suspends the hook until approved, whoever changed it: `PrObserver.changed` triggers the check for an agent's merged PR, and the `SyncObserver` checks the default branch on every sync for the team's own pushes.

### I37: evaluation, reports and promotions (`promotion/contract.py`)

```python
class EvaluationPayload(BaseModel):     # the typed view of EvaluationRecord.payload (S5 §3); schema_version 1; `extra="allow"` so a team's extra fields survive
    schema_version: int; evaluation_commit: str; gate_config_version: str; champion_version: str | None
    decision: Literal["promote", "hold", "reject"] | None; reasons: list[str]
    parity: ParityResult | None; champion_metrics: MetricSet | None; challenger_metrics: MetricSet | None; calibration: dict[str, Any] | None
    expected_cost: float | None; decision_rate: float | None; holdout_window: TrainingWindow | None; training_window: TrainingWindow | None
    serving_compatibility: dict[str, Any] | None; computed_where: str | None
class ParityResult(BaseModel): tolerance: float; mismatch_rate: float
class MetricSet(BaseModel): overall: dict[str, MetricValue]; per_group: dict[str, dict[str, MetricValue]]
class MetricValue(BaseModel): value: float; ci_low: float | None; ci_high: float | None; n: int
def comparable(a: EvaluationPayload, b: EvaluationPayload) -> bool: ...   # same evaluation_commit and gate_config_version; written by promotion task 1 in promotion/evaluation.py

class EvaluationView(BaseModel): record: EvaluationRecord; payload: EvaluationPayload; not_computed: dict[str, str | None]   # field -> the open PR that adds it, or None
class EvaluationReader(Protocol):       # promotion task 1
    async def for_candidate(self, project_id: str, model: str, version: str) -> EvaluationView | None: ...
    async def latest(self, project_id: str, model: str) -> EvaluationView | None: ...

REQUIRED_REPORT_FIELDS: Final[tuple[str, ...]]   # "fix_chosen", "data_window", "leakage", "label_maturity", "imbalance", "training_serving_skew", "risks_and_watch", "rollback_plan"
class ReportField(BaseModel): value: str | None; not_applicable_reason: str | None   # exactly one is set
class ReportDraft(BaseModel): model: str; candidate_version: str; fix_id: str | None; training_request_id: str | None; assessments: dict[str, ReportField]
class ReportView(BaseModel): id: str; draft: ReportDraft; evaluation: EvaluationView | None; created: datetime; author: str   # rep_...
class Reports(Protocol):                # promotion task 2
    async def save(self, project_id: str, draft: ReportDraft, author: str) -> ReportView: ...   # refused when a required field is missing
    async def get(self, project_id: str, report_id: str) -> ReportView | None: ...
    async def for_candidate(self, project_id: str, model: str, version: str) -> ReportView | None: ...

class PromotionPayload(BaseModel): model: str; report_id: str; candidate_version: str; champion_version: str | None; deploy_hook_id: str
class PromotionView(BaseModel): id: str; model: str; candidate_version: str; champion_version: str | None; mode: Literal["team_gate", "person"]; decided_by: Literal["person", "agent_bypass", "team_gate"]; status: Literal["proposed", "applied", "refused", "rejected"]; report_id: str | None; refusals: list[str]; created: datetime; applied_at: datetime | None   # prm_...
class Promotions(Protocol):             # promotion
    async def validate(self, project_id: str, payload: PromotionPayload) -> list[str]: ...   # code checks, used at propose and at approval: report complete, evaluation fresh (`evaluation_max_age_hours`) and by the same code as the champion's, champion unchanged since the evaluation
    async def history(self, project_id: str, model: str | None = None) -> list[PromotionView]: ...
```

- `person` mode: Retraining proposes with `Propose(kind="promotion")` (the kind's `ProposalCheck` runs `validate` first); at approval `validate` runs again, then the model's deploy hook runs (basis `approval`, `params["candidate_version"]`); at `bypass` the same checks run and the apply needs no click. "Fresh" is measured from the end of the record's tracker run. `team_gate` mode: the app never deploys; for runs the app requested it reads the gate's decision and reasons from the evaluation record, notifies the first on-call person (kind `promotion`, `low`, not urgent), and stage 4 watches the new version.
- `GET /api/projects/{id}/reports/{report_id}` returns `ReportView` (promotion task 2): the promotion card and the Deployments page show it.
- The post-deploy comparison: when tracking sees a production alias move (`DeploymentRow`), promotion wakes Deployment & Rollback (`post_deploy`) with the model and versions; the agent compares the record and the live label-based results.

### I38: merging (types in `promotion/contract.py`; `integrations/github/merge.py`, `agents/tools/merge.py`)

```python
class ProtectionSurfaces(Protocol):     # promotion task 7
    async def paths(self, project_id: str) -> list[str]: ...                    # entry files of every model's evaluate and deploy hooks, every file under .github/workflows/, CODEOWNERS (root, .github/, docs/); decided point 5
    async def touches(self, project_id: str, pr_number: int) -> list[str]: ...   # the surface paths the PR changes, from `list_pull_files` (old and new path of a rename); a list shorter than `changed_files` refuses; the agent's labels are never read
class MergeCheck(BaseModel): ok: bool; refusals: list[str]       # own PR, mergeable and clean, no admin override, at least one passing check, no protection surface
```

- `MergePullRequest` is in code: an agent merges only its own PR; the platform reports it mergeable and clean; at least one check passed (a repository without CI never self-merges, nor does a local review); never an admin override; never a PR that touches a protection surface, at any level. Merges write `pr.merged_by_agent`.

### I39: the Dashboard's data (types in `monitoring/contract.py`; `GET /api/projects/{id}/dashboard`, ui task 2)

```python
class StageStatus(BaseModel): name: str; status: Literal["ok", "late", "incident", "unknown"]; incident_ids: list[str]
class ModelCard(BaseModel): name: str; production_version: str | None; latest_metrics: dict[str, float]; open_incidents: int
class AgentActivity(BaseModel): agent: str; state: AgentState; doing: str | None            # stage 2's AgentStatus fields
class DashboardView(BaseModel): stages: list[StageStatus]; edges: list[PipelineEdge]; models: list[ModelCard]; incidents_by_severity: dict[Severity, int]; at_risk: list[ExpectationState]; agents: list[AgentActivity]; heartbeat_reachable: bool; baseline_available: bool
```

### I40: the Deployments page's data (`GET /api/projects/{id}/deployments`, promotion task 6)

```python
class EnvironmentRow(BaseModel): model: str; environment: str; alias: str; version: str | None
class DecisionRow(BaseModel): at: datetime; model: str; kind: Literal["promotion", "rollback", "fall_back"]; from_version: str | None; to_version: str | None; decided_by: Literal["person", "team_gate", "rule", "agent_bypass"]; decided_by_ref: str | None; report_id: str | None   # decided_by_ref: the rule ID or the person
class OpenPipelinePr(BaseModel): number: int; title: str; agent: str; labels: list[str]
class DeploymentsView(BaseModel): environments: list[EnvironmentRow]; decisions: list[DecisionRow]; open_prs: list[OpenPipelinePr]
```

- Environments come from tracker aliases and the handoff's `ModelSpec.environments`; decisions from `Promotions.history` and `Hooks.runs` of kind rollback and fall_back (`rule` with its ID; basis `approval`: `person`; basis `none`, allowed at `bypass`: `agent_bypass`); an alias move with no app record shows in `environments` only.

### I41: Stats (`stats/contract.py`, stats task 3)

```python
STATS_MIN_VERDICTS: Final = 5                                   # a number shows only from this many verdicts (2026-10-29)
StatsWindow = Literal["7d", "30d", "all"]
StatGroup = Literal["detection", "speed", "noise", "diagnosis", "effort", "model_impact", "cost", "reliability"]   # the roadmap's stage 6 groups
STAT_METRICS: Final[Mapping[StatGroup, tuple[str, ...]]]        # the one list of metric names: detection (precision, caught_baseline_missed, false_alarms_avoided, misses), speed (detection_delay_s, time_to_resolution_s), noise (alerts_per_week, signals_per_incident), diagnosis (bad_data_vs_drift_correct), effort (autonomous_share, approvals_requested, acceptance_rate, prs_opened, prs_merged, prs_closed), model_impact (metric_before, metric_after), cost (tokens_per_agent, cost_per_incident), reliability (failed_runs, tool_errors, sandbox_failures)
class StatValue(BaseModel): value: float | None; n: int; collecting: bool          # n: the verdicts (or incidents, for counts) it rests on; collecting: n < STATS_MIN_VERDICTS for a verdict-based number
class StatsView(BaseModel): window: StatsWindow; category: ResultCategory | None; baseline_available: bool; groups: dict[StatGroup, dict[str, StatValue]]
class Stats(Protocol):                  # stats
    async def compute(self, project_id: str, window: StatsWindow, category: ResultCategory | None, now: datetime) -> StatsView: ...   # computed on read from incidents, verdicts, baseline links, approvals, PRs, llm_calls, hook runs; nothing stored twice
```

- `GET /api/projects/{id}/stats?window=&category=` returns `StatsView`. Without readable team monitoring `baseline_available` is false and the page says so (S4 §10); the detection numbers that need the baseline are then `None`, never 0.

### I42: Usage (types in `providers/usage_contract.py`; `providers/usage.py`, ui task 1)

```python
class ModelPrice(BaseModel): prompt_per_mtok: float; completion_per_mtok: float; source: Literal["provider", "user"]   # USD per million tokens
class UsageRow(BaseModel): project_id: str; agent: str; model_address: str; week_start: date; prompt_tokens: int; completion_tokens: int; cost_usd: float | None   # None: no price for that model
class UsageView(BaseModel): rows: list[UsageRow]; unpriced_models: list[str]
def cost_usd(prompt_tokens: int, completion_tokens: int, price: ModelPrice | None) -> float | None: ...   # ui task 1, providers/usage.py; the one tokens-to-dollars function (Usage and Stats)
```

- `GET /api/usage?since=` (app level) sums `llm_calls` of every project by agent, model and week (weeks start Monday 00:00 UTC).
- `ModelEntry` gains `price: ModelPrice | None` (columns in `app/0003_stage4_5.sql`, contracts task 2; read and written by ui task 1); `PUT /api/models/price` (body: `address`, `price` or null) sets or clears a user price, as `PUT /api/models/context-window` takes its address in the body. When a provider's model list publishes prices (OpenRouter's `/models`: `pricing.prompt` and `pricing.completion`), the price is stored with `source="provider"` when such a model is added or verified, and when `list_remote_models` reads the list; a user price is never overwritten.

### Routes (each in its owner's router file; `{id}` is the project; the ui consumes them)

| Route | Plan, task |
|---|---|
| `GET`/`PUT .../monitoring-settings` (`MonitoringSettings`) | contracts 2 |
| `GET /api/health/heartbeat` (`HeartbeatView`, no session) | results 3 |
| `GET .../expectations` (`list[ExpectationState]`); `GET .../checks` (`list[CheckView]`) | results 5; results 7 |
| `GET .../incidents?status=&limit=&offset=` (`Page[IncidentView]`, ranked), `GET .../incidents/{inc}`, `.../signals`, `.../timeline?before=&limit=` (`list[EventRecord]`, newest first), `POST .../incidents/{inc}/acknowledge`, `.../close` | incidents 1 |
| `POST .../incidents/{inc}/snooze` (`{until}`); `GET`/`POST .../maintenance-windows`, `DELETE .../maintenance-windows/{win}` | incidents 7 |
| `GET`/`PUT .../on-call` | notifications 2 |
| `GET`/`PUT /api/integrations/discord`, `.../email`, `.../public-url`; `DELETE .../discord/webhook`, `.../email/password`; `POST .../{discord,email}/test` | notifications 3 |
| `GET .../hooks`; `PUT`/`DELETE .../hooks/{hok}/credential`; `PUT .../hooks/{hok}/enabled` | hooks 2 |
| `GET .../rules?model=`; `PUT .../rules/{rul}/enabled` | hooks 5 |
| `POST .../hooks/{hok}/approve` (person only) | training 7 |
| `GET .../training/policies`; `PUT .../training/policies/{pol}/enabled` | training 8 |
| `GET .../reports/{rep}`; `GET .../deployments` | promotion 2; promotion 6 |
| `POST .../incidents/{inc}/verdict`, `GET .../verdicts?since=`, `POST`/`GET .../misses` | stats 1 |
| `GET .../incidents/{inc}/baseline`, `GET .../baseline/silent-alerts`, `POST .../baseline/silent-alerts/{res}/verdict` | stats 2 |
| `GET .../stats?window=&category=` | stats 3 |
| `GET /api/usage?since=`, `PUT /api/models/price`; `GET .../dashboard`; `GET .../activity?type=&source=&before=&limit=` | ui 1; ui 2; ui 5 |

## Spec coverage

Every section of both specs, the roadmap's stage 6 and the numbered changes, with the plan that builds it.

| Spec | Plan |
|---|---|
| S4 §1 layout | contracts (packages); each plan its module |
| S4 §2 record, sources, parsers, cursors, catching up | results; untrusted: contracts, incidents (`MonitoringQuery`), definitions; "resolved while offline": incidents task 3 |
| S4 §3 scheduler and jobs, §3b heartbeat | results (loop, route); each job by its owner; definitions (the heartbeat job in the team's scheduler); ui (local-mode notice) |
| S4 §3a expectations and silence | results task 5; maintenance: incidents task 7 |
| S4 §4 checks by agents, `RegisterCheck` | results task 7; check content: definitions |
| S4 §5 signals, grouping, lifecycle, quiet period, windows, snooze, tools | incidents |
| S4 §6 waking, the menu, recent changes | incidents (waking, suspects), training (menu 6), hooks (menu 3 to 5), definitions (menu, reverts, fixing causes) |
| S4 §6 hooks, credentials, hold; rules | hooks |
| S4 §6 post-deploy comparison, humans decide deploys | promotion (trigger), definitions (comparison), contracts and hooks (gate) |
| S4 §7 severity, routing, escalation, Alert Prioritization | incidents (severity, routing, the immediate deliveries, AP); escalation: notifications task 4 |
| S4 §8 channels, links, on-call, report, failures | notifications; UI: ui task 4 |
| S4 §9 proposal kinds | contracts (payloads, names); each kind and card by its owner: results (`parser`), hooks (`hooks`, `rule`), incidents (`maintenance`), training (`training_policy`), promotion (`promotion`) |
| S4 §10 verdicts, labels, baseline | stats; buttons and the miss report: ui task 3; silent-alert verdicts: stats task 4 |
| S4 §10 Knowledge note per resolved incident | definitions (decided point 8) |
| S4 §11 UI | ui |
| S4 §12 storage, roadmap changes | contracts; heartbeat exception: results |
| S4 §13 errors | results, hooks, notifications; scrubbing: all |
| S4 §14 testing, Done when | each plan its tests; the simulated night: definitions task 4 |
| S5 hard limits, `MergePullRequest` | promotion task 7 (merging); default branch only and pins: training; cells: contracts |
| S5 the principle, PR discipline | definitions (prompts); merge rules in code: promotion task 7 (decided point 9) |
| S5 changes 1 to 6 | definitions (shadow, canary by PR), contracts (cells, override, keyed by model), training (`GateFacts`, policy authority), ui (a list of models) |
| S5 §1 fix requests, guards, breaker | training tasks 1, 2, 6 |
| S5 §2 hooks: placeholders, `ci_dispatch`, `deploys` verified; policy; linking back | training |
| S5 §3 evaluation record, comparability | promotion; the step in the team's pipeline: definitions |
| S5 §4 promotion modes, report | promotion |
| S5 §5 agents' instructions | definitions |
| S5 §6 UI (Deployments page) | data: promotion task 6; page: ui task 7 |
| S5 §7 storage | contracts |
| S5 §8 testing | each plan; end to end: definitions task 5 |
| Roadmap stage 6: Stats | stats (I41) |
| Roadmap stage 6: Activity, Knowledge, Usage | ui tasks 5, 6, 1 (I42) |

## Decided points

The lead took every recommended default on 2026-10-29 (`project_knowledge.md`, Process). #16 and #21 change stage 3 interfaces (I19, I20) and are recorded by Decision issues.

1. **Origin of `ci` and `mlflow` records.** Default: add origin `tracker` for records the app derives from the tracking record; they raise signals. `team` file records only feed the baseline; a team job's silence still raises through expectations.
2. **Gate cells the roadmap leaves open** (the "?" cells of I30). Default: as in the table: `train`/`evaluate` and a deploying run are denied at `suggest` and ask without authority even at `bypass`; `hold` asks at `suggest` (the one click). Promotion in `person` mode needs no click at `bypass` (roadmap and S5 change 3).
3. **Tools the specs need but do not name** (`IncidentFollowUp`, `AdjustSeverity`, `Hold`, `RecordFixChoice`, `SaveReport`, `MonitoringQuery`, the name `RunHook`). Default: add them as in I33.
4. **Which feeds production depends on** (severity). Default: derived from the pipeline graph (a feed whose stage has a path to a model's stage); no new handoff field.
5. **Protection surfaces.** Default: the entry files of each model's evaluate and deploy hooks, every file under `.github/workflows/`, and `CODEOWNERS`. (Simpler than reading the branch protection API, and covers it.)
6. **A signal's subject to a pipeline stage.** Default: exact name match against `PipelineStage.inputs` and `outputs`, else grouping by cause key only.
7. **Two sources of hooks.** Default: the approved handoff's `commands` at `handoff.approved`, later ones through the `hooks` proposal; approving changed hook content is a person-only route.
8. **Who writes the Knowledge note per resolved incident.** Default: the diagnosing agent, told by its definition.
9. **PR discipline** (open-PR cap, `decision-semantics` label). Default: prompts only; code enforces only the merge rules and the closed-unmerged note.
10. **System wakes are outside stage 2's hop and budget limits.** Default: keep them outside, but cap them at `MAX_WAKES_PER_HOUR = 30` per project (one constant, in `monitoring/settings.py`); a wake over the cap is recorded as an `incident.wake_skipped` event on its incident (shown on the incident page and in Activity), and the morning report lists it too when that task exists.
11. **The `team_gate` report** has no tracker write path. Default: store it as a `rep` record and show it; the team's pipeline writes its own.
12. **Milestones.** Default: as in the Plans table (Stage 4: contracts, results, incidents, notifications, hooks, ui, definitions; Stage 5: training, promotion; Stage 6: stats).
13. **Shared working rules.** Default: the definitions reference the packaged working-rules text stage 3 onboarding adds; they do not repeat it.
14. **Template filling.** Default: the stage 3 grammar: parse with `string.Formatter`, fill with `str.format_map`, for `command` templates and `ci_dispatch` inputs alike.
15. **Cycles in the pipeline graph.** Default: downstream search and the "production depends on" path keep a visited set.
16. **`ProposalView.payload` is the closed union of stage 3's two payloads (I19).** Default: contracts task 4, which adds the six payloads that need it, widens it to a union discriminated by kind, and starts only after the stage 3 proposals plan has merged, so the proposal cards in progress are not built against a moving type (a Decision issue records the I19 change).
17. **Stage 6 without a spec.** Default: the roadmap's stage 6 bullets, the 2026-10-29 decisions and I41/I42 are its spec; no separate spec document.
18. **Where a model's price lives.** Default: on the model entry in `app.db` (I42), filled from the provider's model list when it publishes prices (OpenRouter), else by the user; a user price wins.
19. **Maintenance before incidents task 7.** Default: contracts ships a `Maintenance` default that never suspends, so expectations (results task 5) need not wait for the extras; incidents task 7 registers the real one.
20. **Ten plans instead of nine.** Default: notifications stays its own plan (see Plans).
21. **How stage 4 learns a handoff was stored.** Nothing on `main` lets backend code react to it (`handoff.approved` is an event for the UI stream, emitted by stage 3 onboarding). Default: the `HandoffObserver` seam on `Handoffs` (Shared by contracts), plus a reconcile of `Handoffs.current()` at startup; a Decision issue records the I20 change.

## Files shared by these plans

Each file is edited by one plan only, except where a line says otherwise:
Paths are under `backend/src/mlops_agents/` unless they start with `backend/` or `frontend/`.
- contracts: both migrations, `storage/tables.py`, the schema-pin tests (`tests/storage/test_migrate.py`, `test_stage2_schema.py`, `test_stage3_schema.py`: the version lists), `ids.py`, `config.py` and `backend/pyproject.toml` (`croniter`), `credentials/project_secrets.py` (`hook_ref`), `events/contract.py`, `events/log.py` and `events/audit.py` (`EventType`, the two `page` reads), `frontend/src/lib/useProjectEvents.ts` (`PROJECT_EVENT_TYPES`), `agents/policy/table.py`, `agents/policy/actions.py`, `agents/approvals/gate.py`, `agents/approvals/authority.py`, `agents/runtime/hooks.py` (`GateCall.action`, `GateDecision.authority`), `agents/runtime/tool_step.py` (the line passing the authority), `agents/tools/base.py` (`action_call`, `ToolContext.authority`), `agents/tools/names.py`, `agents/tools/propose.py`, `agents/hooks/registry.py` (its data block through `untrusted`), `agents/pr_sync.py` (`PrObserver`), `integrations/github/pulls.py` (`merged_at`, `changed_files`, `list_pull_files`), `proposals/contract.py`, `proposals/handoffs.py` (`add_observer` only), `proposals/service.py` (the view's construction and the apply of an allowed propose only), `api/routers/monitoring_settings.py`, `monitoring/settings.py`, `monitoring/jobs.py`, `monitoring/untrusted.py`, `monitoring/expr.py`, `monitoring/query.py`, `monitoring/contract.py`, `providers/usage_contract.py`, every `contract.py` of I22 to I41; in `backend/tests/fakes/fake_github.py`, `merged_at` and the PR files route. A later plan that needs a change opens a Decision.
- results: `monitoring/results/`, `monitoring/scheduler/`, `monitoring/expectations/`; `api/routers/heartbeat.py`, `api/routers/expectations.py`; `api/security.py` (the heartbeat's `_OPEN` entry); `api/app.py` (the `clock` parameter); `tracking/loop.py` and its tests and `build_services` lines (removed: the `SYNC_TRACKING` job replaces it); `backend/tests/fixtures/results/`.
- incidents: `monitoring/incidents/` (severity and routing included), `monitoring/waker.py`, `api/routers/incidents.py`; `agents/runtime/sessions.py` and `agents/runtime/contract.py` (only `AgentSessions.post`'s `system` keyword, the system wake's exception to the chat check).
- notifications: `notifications/`, `api/routers/integrations.py`, `api/routers/on_call.py`; `api/hosts.py` (only the heartbeat's `public_url` host); `backend/tests/fakes/fake_discord.py`, `fake_smtp.py`.
- hooks: `monitoring/hooks/` except the files training owns; `monitoring/rules/`; `api/routers/hooks.py`, `api/routers/rules.py`; `proposals/templates.py` (`fill_template` only); `sandbox/containers.py` (only the one-shot container run, which results' code parsers reuse).
- training: `training/`; `monitoring/hooks/placeholders.py`, `ci_dispatch.py`, `pins.py`, `alias_check.py`, `facts.py`; `integrations/github/dispatch.py`; `api/routers/training.py`; in `backend/tests/fakes/fake_github.py` and `fake_mlflow.py`, its own functions only.
- promotion: `promotion/`; `integrations/github/merge.py`, `agents/tools/merge.py`, `agents/tools/save_report.py`, `api/routers/promotion.py`; in `backend/tests/fakes/fake_github.py`, its own functions (check runs, `mergeable`, open PR labels) and the `sha` check of stage 2's merge route. Neither training nor promotion edits `integrations/github/api.py`.
- stats: `monitoring/verdicts/`, `stats/`, `api/routers/{verdicts,baseline,stats}.py`, `frontend/src/pages/stats/`.
- ui: `monitoring/dashboard.py`, `events/activity.py`, `api/routers/{dashboard,activity,usage}.py`, `providers/usage.py`, and for the price `providers/listing.py`, `providers/models.py`, `providers/service.py`, `api/routers/models.py` (as they are on `main`); in `backend/tests/fakes/fake_openai.py`, the optional `pricing` per listed model; `frontend/src/pages/{dashboard,incidents,activity,knowledge,usage,deployments}/`, `frontend/src/pages/providers/ModelsTable.tsx`, `frontend/src/pages/project-settings/ProjectSettingsPage.tsx` and `frontend/src/pages/integrations/IntegrationsPage.tsx` (one line per section), `frontend/src/pages/inbox/InboxPage.tsx`.
- definitions: `agents/definitions/*.md` (after stage 3 onboarding), `backend/tests/agents/test_agent_definitions.py` and the tool-set constants in `backend/tests/conftest.py` (its lines), `backend/tests/e2e/`, `backend/tests/fakes/monitoring_script.py`, its scripted models in `backend/tests/fakes/fake_openai.py`, `backend/tests/api/conftest.py` (`keystore` and `clock` parameters of `build_app` and `started_app`), `frontend/e2e/monitoring.spec.ts`, `frontend/e2e/actions.spec.ts`, its lines in `frontend/playwright.config.ts` and `frontend/e2e/env.ts`.
- Shared, each plan only its own lines: `agents/tools/__init__.py` (`default_registry`), `api/services.py` (every kind, job, runner, lifecycle, authority, observer and query provider is registered in `build_services` after the registries exist), `frontend/src/pages/agents/ApprovalCard.tsx` (one dispatcher line per proposal kind; the card in its own file under `frontend/src/pages/agents/proposals/`). New pages carry their own `route.tsx` and nav entry.
- `frontend/openapi.json`: the plan that changes the API regenerates it.
