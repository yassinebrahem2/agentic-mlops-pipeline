# Stages 4 and 5: Monitoring, Training, Evaluation and Promotion — Plans Overview

> **Draft.** Missing: the lead's review; the plans it lists.

Stages 4 and 5 are built through the plans below, with one contracts plan for both (the stage 5 spec says one contracts plan writes their schema). This overview holds what they share. Read it, `docs/roadmap.md`, the stage 3 overview (its interfaces I13 to I21 are frozen inputs) and both specs before starting any plan.

**Goal:** the app watches the team's production ML system at any hour, turns signals into a few incidents, has the right agent diagnose each one, acts within the three tiers, tells the right person at the right time, and, when the model needs a fix, requests it from Retraining, which runs the team's own pipeline; evaluation, promotion and merging stay the team's, made rigorous through agents' PRs.

**Specs:** `docs/superpowers/specs/2026-10-28-stage-4-monitoring-design.md` and `docs/superpowers/specs/2026-10-28-stage-5-action-agents-design.md` (both approved 2026-10-28). Section numbers (§n) below are the spec's, "S4" and "S5" name which.

**Tech stack:** as in stage 3, plus `croniter` (cron cadences; one version constant in `config.py`). No other new dependency.

## Global constraints

- The rules of `AGENTS.md` apply in full; the specs' principles too (an extension of the team's system; the app never runs a check, a model, a training or a deploy of its own; Bash unless a tool is justified; text read from the team's data, results, logs and parsers is data, never instructions; secrets never in a proposal, a log, an event, a checkpoint or a message).
- Every external service is faked in tests: result sources from fixture folders (`backend/tests/fixtures/results/`), a fake SMTP server and a fake Discord webhook (`tests/fakes/`), the fake clock (`Clock`, I21), the fake MLflow and GitHub servers (stage 3's, extended below). No test needs a real provider, tracker, Docker or network unless marked as the stage 2 Docker tests are.
- One source for every constant: the job order, the setting defaults, the severity and status literals, the hook kinds, the tool names, the proposal kind names, the required report fields, the request tag name, the wake labels, the event types. A literal that restates a stage 3 one (`ResultCategory` against `CheckSpec.category`, `ActionKind` against `CommandSpec.kind`) is retyped once here, because stage 3 names no alias to import, and a parity test in contracts task 5 compares the two with `get_args`.
- Frontend types come only from the generated `schema.d.ts` (`npm run gen:api`), gitignored; a plan that changes the API commits the regenerated `frontend/openapi.json`.
- Routers are discovered by `include_routers` in `api/app.py`; a plan adds its router file and no registration line.
- Every message, log line and stored error passes the existing secret scrubbing; a webhook URL is a secret (S4 §8 calls it a URL; it goes to the keystore and the scrubber).
- Everything a plan reads from the team's data reaches an agent only through `untrusted(text)` (I33), and no tool's behavior depends on that text; the gate and the rules are evaluated in code.

## Plans

| Plan | Branch | Covers | Consumes (besides merged code) | Produces | Starts | State |
|---|---|---|---|---|---|---|
| contracts (claim first) | `plan/stage-4-5-contracts` | S4 §1, §9, §12 and its roadmap changes (IDs, gate inputs); S5 §7 and its roadmap changes, change 3 and 4's gate cells; I22 to I38 as models and protocols; both migrations; `Condition`; the six proposal payloads; the gate's second table | stage 3: I13, I16, I18, I19, I20, I21 | I22 to I38 in code, the schema, the gate seam | after stage 3 contracts task 2 | Planned |
| results | `plan/stage-4-results` | S4 §2 (records, sources, parsers, cursors, catching up), §13 sources and parsers, `parser` kind; S5 §3 (the evaluation copy) | I22, I23, I24 (task 1); I16; I19; stage 3 connections task 1 | `Results`, `Parsers` (I22) from task 1 (early PR) | task 1 after contracts tasks 1 and 2; the rest also after scheduler task 1 and stage 3 tracking task 1 and proposals task 1 | Planned |
| scheduler | `plan/stage-4-scheduler` | S4 §3 (the loop, the job table, catching up, the timer jobs), §3b the heartbeat route, the run-mode exception, times (DST) | I24; stage 3 I21's `Clock` | `Scheduler` and `JOB_ORDER` from task 1, times from task 2 (both early PRs), `SyncObserver` from task 3 | task 1 after contracts tasks 1 and 2 and stage 3 rate-limits task 1 (`Clock`); task 3 after stage 3 tracking task 1 | Planned |
| expectations | `plan/stage-4-expectations` | S4 §3a (expectations, silence), §4 `RegisterCheck` and check activation, S5 §2's request expectation | I22, I24, I25, I26 | `Expectations` (I25) from task 1 (early PR) | task 1 after contracts tasks 1 and 2 and scheduler task 2; tasks 2 to 4 after incidents task 1 and stage 3 proposals task 1 (`handoff.approved`) | Planned |
| incidents | `plan/stage-4-incidents` | S4 §5 (signals, grouping, lifecycle, quiet period, windows, snooze, tools), §6 recent-change suspects, `maintenance` kind, `MonitoringQuery`, §10's "closes after the verdict" hook | I22, I24, I26, I27 | `Incidents` (I26), `Maintenance` from task 1 and 3 (early PRs) | task 1 after contracts tasks 1 and 2; task 3 also after stage 3 proposals task 1 | Planned |
| severity | `plan/stage-4-severity` | S4 §7 (severity, routing, escalation), §6's wake of the diagnosing agent, Alert Prioritization's run, the issue threshold | I26, I27, I28; stage 2 messaging and GitHub issue tools | `AgentWaker` (I27) from task 1 (early PR), `SeverityPolicy`, `Router` | task 1 after contracts task 1 (no table); the rest after contracts task 2, incidents task 1 and notifications task 1 | Planned |
| notifications | `plan/stage-4-notifications` | S4 §8 (channels, links, on-call, retry, failure, the morning report) | I24 (times), I26, I28 | `Notifier`, `OnCallPeople` (I28) from tasks 1 and 3 (early PRs) | task 1 after contracts tasks 1 and 2; task 3 after stage 3 proposals task 1; task 5 after incidents task 1 and verdicts task 1 | Planned |
| hooks | `plan/stage-4-hooks` | S4 §6 (action hooks, hook credentials, hold, failures recorded), `hooks` kind, `RunHook`, `Hold` | I29, I30, I26; stage 3 proposals task 1 (`handoff.approved`, `Handoffs`) | `Hooks` (I29) and its seams from task 1 (early PR) | task 1 after contracts tasks 2, 3 and 4; tasks 2 and 3 after incidents task 1 | Planned |
| rules | `plan/stage-4-rules` | S4 §6 (pre-approved rules, evaluated in code), `rule` kind | I22, I23, I29, I30 | the rule `ActionAuthority` | after contracts tasks 2, 3 and 4, results task 1 and hooks task 1 | Planned |
| verdicts | `plan/stage-4-verdicts` | S4 §10 (verdicts, misses, labels, the baseline record), §14's verdict tests | I22, I26, I31 | `Verdicts`, `Baseline` (I31) from task 1 (early PR) | task 1 after contracts task 2 and incidents task 1; tasks 2 and 3 also after results task 1 and scheduler task 3 (`SyncObserver`) | Planned |
| fixes | `plan/stage-5-fixes` | S5 §1 (`RequestFix`, the fix menu's record, the guards), the circuit breaker (§2), change 5 | I26, I27, I34; I20 (profile, label maturity) | `Fixes` and its guards (I34) from task 1 and 2 (early PRs) | task 1 after contracts task 2 and severity task 1; task 2 after incidents task 1 | Planned |
| pipeline-runs | `plan/stage-5-pipeline-runs` | S5 §2 hooks part: placeholders, `ci_dispatch`, pins, the alias-history check, the production-signal rule, the gate's verified facts; changes 4 | I29, I30, I36; I16; the GitHub client | `PlaceholderResolver`, `CiDispatcher`, `HookVerifier` (I36) from task 1 (early PR) | task 1 after hooks task 1 and contracts task 3; the rest also after stage 3 tracking task 1 and scheduler task 3 (`SyncObserver`) | Planned |
| training | `plan/stage-5-training` | S5 §2 training part: training policies, training requests, linking back; `training_policy` kind | I34, I35, I36, I25; I16 (`ci_runs`) | the policy `ActionAuthority`, `Training` (I35) | task 1 after contracts tasks 2 and 4; the rest after fixes task 2, pipeline-runs task 1, expectations task 1 and scheduler task 3 | Planned |
| promotion | `plan/stage-5-promotion` | S5 §3 (the evaluation record read, comparability), §4 (reports, `promotion` kind, both modes), the post-deploy comparison's trigger; changes 1 and 2's code side | I16 (`EvaluationRecord`), I29, I36, I37 | `Reports`, `Promotions`, `EvaluationReader` (I37) from task 1 and 2 (early PRs) | task 1 after contracts tasks 1 and 2 and stage 3 tracking task 1; tasks 3 and 4 after hooks task 3, pipeline-runs task 1 and scheduler task 3 | Planned |
| merging | `plan/stage-5-merging` | S5 "Hard limits and merging" (`MergePullRequest`, protection surfaces), the PR discipline's code parts | I30, I38; stage 2 pull request tools; the GitHub client | `MergePullRequest`, `ProtectionSurfaces` (I38) | after contracts task 3 and hooks task 1 | Planned |
| dashboard | `plan/stage-4-dashboard` | S4 §11: Dashboard (S5 change 6: a list of models), Inbox incidents, the incident page, verdict buttons and the miss report | I26, I25, I31, I39; stage 3 tracking routes | the Dashboard route and pages | route after incidents task 1 and expectations task 1; pages after the route and verdicts task 1 | Planned |
| settings | `plan/stage-4-settings` | S4 §8 and §11: the Integrations sections for Discord and email, the project settings sections (on-call, windows, rules, hooks, policies, intervals, thresholds, time zone, `public_url`) | the routes of notifications, incidents, rules, hooks, training | the sections | after contracts task 2 and each section's routes are on `main` | Planned |
| deployments | `plan/stage-5-deployments` | S5 §6 (the Deployments page, the promotion card) | I37, I40; I16; stage 3 tracking routes | the page, the card | route after promotion task 2; page and card after the route | Planned |
| definitions | `plan/stage-4-5-definitions` | S4 §4, §6 and S5 §5 as instructions: all seven definitions; the stage 4 and 5 end-to-end tests ("simulated night" included) | every plan above | the working monitoring loop and fix flow | tasks 1 and 2 after every plan above that registers a tool (the catalog validates `tools:`) and after stage 3 onboarding merges; tasks 3 and 4 after every plan above has merged | Planned |

**contracts' tasks:**
- Task 1 (early PR): "Shared by contracts" below, and the models and protocols of I22 to I38 (types only; behavior stays with the plans). Every plan starts on it.
- Task 2 (early PR): the two migrations and `tables.py`.
- Task 3 (early PR): the gate seam (I30): `ActionCall`, `ActionAuthority` and its registry with the "not within a rule or policy" defaults, the second policy table, the per-kind `propose` override for `promotion`, `GateCall.action`, `AgentTool.action_call`. Behavior of the existing policy is unchanged until a plan registers an authority.
- Task 4 (early PR): `monitoring/expr.py` (`Condition`, `evaluate`) and the six proposal payloads (I32) with their validators.
- Task 5: the consistency tests (every table has the columns its model names; the backend and frontend event lists agree; the literals that restate stage 3's, `ResultCategory` and `ActionKind`, equal them by `get_args`) and the regenerated `openapi.json` if a route landed.

**Task outlines** (each plan's plan file writes them out in full; at most 5 each):
- results: 1 the record, `Results` store, idempotent ingest, query (early PR); 2 the `file` source and cursors; 3 the `ci` and `mlflow` sources, `collect` in cursor order, the `collect_results` job, the evaluation copy (`EvaluationCopies`); 4 the declarative parser, the versioned `Parsers` store, the `parser` kind's apply, disabling after N failures with a signal; 5 the code parser in the sandbox, the parser card.
- scheduler: 1 the job table, the loop, keyed idempotent jobs, catching up in `JOB_ORDER`, start and stop (early PR); 2 times: `next_occurrences`, `in_hours`, DST (early PR); 3 the `sync_tracking`, `sync_prs` and `after_sync` jobs and `SyncObserver`; 4 `GET /api/health/heartbeat`, the run-mode exception, the `public_url` Host rule, the local-mode flag.
- expectations: 1 the store and `Expectations` (early PR); 2 the seeding from the handoff on `handoff.approved`, deadlines, grace, the second deadline, silence signals, the `check_expectations` job, suspension by maintenance; 3 `RegisterCheck`, the `checks` store, activation on PR merge, the never-ran silence; 4 `note_seen` from results, retire, the request expectation API for training.
- incidents: 1 signals, incidents, `Incidents.raise_signal`, lifecycle, observers, timeline events (early PR); 2 grouping (cause key, window, downstream), quiet period, reopen; 3 maintenance windows, snooze, the `maintenance` kind and card, `Maintenance` (early PR); 4 `IncidentUpdate`, `IncidentMerge`, `IncidentSplit`, `IncidentFollowUp`, the follow-ups job, `MonitoringQuery` and its provider registry; 5 "resolved while the app was offline", recent-change suspects (agents' merges by `PrObserver`, the team's merges and deploys by a `SyncObserver`).
- severity: 1 `AgentWaker` (early PR); 2 `SeverityPolicy` (deterministic); 3 `Router`, on-call selection and the reactor that pages at once; 4 escalation, the severity-rise re-notification; 5 the diagnosing-agent dispatch, Alert Prioritization's run, `AdjustSeverity`, the issue threshold.
- notifications: 1 the channels, `Notifier`, retry, `notification_log`, the fake SMTP and Discord servers (early PR); 2 the Integrations settings routes, the keystore refs, Test, links and the local-mode sentence; 3 `OnCallPeople`, routes, seeding from the handoff (early PR); 4 repeated failure into a `notification_failure` signal; 5 the morning report and its job.
- hooks: 1 the `Hooks` store, the registration from the handoff and the `hooks` kind, the runner and lifecycle seams (early PR); 2 hook credentials (route, `hook_ref`, injection into one container run) and the `command` runner; 3 `RunHook`, `Hold`, the missing-hold-command gap, results recorded on the incident; 4 the hooks routes and card.
- rules: 1 the `Rules` store, the `rule` kind, routes, card; 2 the evaluator and limits; 3 the rule `ActionAuthority` and the timeline record.
- verdicts: 1 verdicts and miss reports, routes (early PR); 2 confirmation by labels; 3 the baseline record and the silent-alerts list; 4 the read side for stage 6 and the events.
- fixes: 1 `RequestFix`, `RecordFixChoice`, the store, the wake of Retraining (early PR); 2 the guards (early PR); 3 the circuit breaker, escalation, decline back to observe; 4 the outcome of each fix against its incident.
- pipeline-runs: 1 `PlaceholderResolver` (early PR); 2 `CiDispatcher` and the `ci_dispatch` runner (adds the fake GitHub's dispatch); 3 pins, hashes, suspension, the person's approve route; 4 the alias-history check, the incident, the suspension; 5 the gate's verified facts (`deploys`, production signal, authority) registered with the gate.
- training: 1 the `training_policy` kind, store, routes, card; 2 the policy `ActionAuthority` (interval, runs per week from CI history, rollback-rule requirement); 3 `TrainingLifecycle` (the request `trq`, guards, window, `request_id`); 4 linking back by the tag, the request expectation, the silence.
- promotion: 1 `EvaluationReader`, `comparable`, "not computed" (early PR); 2 `Reports`, required fields, `SaveReport` (early PR); 3 the `promotion` kind, `person` mode, re-validation, the deploy hook as apply; 4 `team_gate` mode and the post-deploy comparison trigger; 5 `Promotions` history and rollbacks for the page.
- merging: 1 `MergePullRequest` and its code checks; 2 `ProtectionSurfaces`; 3 the closed-unmerged observer and the merge events.
- dashboard: 1 `GET .../dashboard`; 2 the Dashboard page; 3 Inbox incidents (rank, acknowledge, snooze); 4 the incident page, verdict buttons, the miss report.
- settings: 1 Integrations: Discord and email with Test; 2 project settings: on-call, intervals, thresholds, time zone, `public_url`; 3 windows, rules, hooks (credential, enable, approve changed content) and policies.
- deployments: 1 `GET .../deployments`; 2 the page; 3 the promotion card.
- definitions: 1 Data Quality, Model Performance, Alert Prioritization; 2 Retraining, Deployment & Rollback, Orchestrator, Onboarding; 3 the stage 4 end-to-end test (the simulated night); 4 the stage 5 end-to-end test.

Owners claim plans as `AGENTS.md` says; contracts first, since every other plan starts on its early PRs. There are 19 plans for six people: an owner whose next task waits on another plan may claim a second plan (`AGENTS.md`). Suggested waves: contracts; then results, scheduler, incidents, notifications, hooks; then expectations, severity, rules, verdicts, fixes, pipeline-runs, promotion, merging; then training, dashboard, settings, deployments; then definitions. Stage 5 plans need only the stage 4 interfaces named in their Starts.

## Interfaces between plans

contracts writes these into code exactly as here (except where a line names another producer); changing one takes a Decision issue. Fields are pydantic models unless marked Protocol; every payload model is `extra="forbid"`. `Page` is I16's, `Clock` is I21's (rate-limits task 1: `providers/failover.py`; `clock.py` has only `now_iso`), `Cadence`, `Feed`, `OnCall`, `CheckSpec`, `CommandSpec`, `FileResults`, `MlflowResults`, `Profile`, `ModelSpec`, `PipelineEdge` are I19's. Changelog: none yet.

### Shared by contracts (task 1)

- ID prefixes `res`, `sig`, `inc`, `rul`, `hok`, `win`, `chk`, `fix`, `trq`, `pol`, `rep`, `prm` in `ids.py` (`inc` is in the roadmap but not yet in `ids.py`).
- Event types, in the backend `EventType` (`events/contract.py`) and the frontend `PROJECT_EVENT_TYPES`:
  - stage 4: `results.collected`, `signal.raised`, `incident.opened`, `incident.updated` (payload: the changed fields), `incident.acknowledged`, `incident.snoozed`, `incident.severity_changed`, `incident.reopened`, `incident.merged`, `incident.split`, `incident.suspect_linked`, `incident.hold_recorded`, `incident.closed`, `check.registered`, `parser.disabled`, `notification.sent`, `notification.failed`, `escalation.sent`, `report.sent`, `hook.ran`, `hook.failed`, `hook.suspended`, `rule.satisfied`, `verdict.recorded`, `miss.reported`;
  - stage 5: `fix.requested`, `fix.chosen`, `fix.declined`, `fix.escalated`, `training.requested`, `training.linked`, `policy.suspended`, `report.saved`, `promotion.applied`, `promotion.refused`, `pr.merged_by_agent`, `evaluation.stored`.
  - A timeline entry of an incident is an event whose `subject_id` is the incident ID; actions on hooks, rules and notifications carry the incident as `subject_id` when there is one, so every entry is recorded once.
- Constants in one module per concern, each with a single definition: `monitoring/settings.py` (`MonitoringSettings`, below), `monitoring/jobs.py` (`JOB_ORDER`, I24), `agents/tools/names.py` (every tool name of I33), `monitoring/untrusted.py` (`untrusted(text: str) -> str`, which wraps text in the marker block the agents' prompts name), `training/constants.py` (`REQUEST_TAG = "mlops_agents.request_id"`, the tag the team's pipeline writes on its MLflow run; `EVALUATION_ARTIFACT = "mlops_agents/evaluation.json"` is stage 3's), `promotion/constants.py` (`REQUIRED_REPORT_FIELDS`, I37).
- `MonitoringSettings` (project settings, defaults in one place, stored in `monitoring_settings`): `collection_interval_s=60`, `grouping_window_minutes`, `reopen_window_hours`, `recent_change_window_hours`, `escalation_timeout_minutes`, `parser_disable_after`, `evaluation_max_age_hours`, `circuit_breaker_fixes`, `circuit_breaker_window_days`, `timezone: str | None` (None = the server's). App settings (`app.db`): `public_url: str | None`, `DiscordSettings`, `EmailSettings` (I28).
- `credentials/project_secrets.py` gains the one builder `hook_ref(project_id, hook_id)` (ref `project/<id>/hook/<hok>`, under `project_prefix`, so project removal deletes it) and `hook_prefix`; the app-level refs `DISCORD_WEBHOOK_REF` and `EMAIL_PASSWORD_REF` are constants beside the GitHub token's. No other code writes a ref string.
- `PrObserver` (Protocol, `agents/pr_sync.py`): `async changed(project_id, pr: PrChange)`, with `PrChange(number: int, state: Literal["open","merged","closed"], merged_at: datetime | None, head_branch: str, agent: str, files: list[str])` (stage 2's sync sees only agents' task PRs, so `agent` is always set; the team's own merges and deploys reach incidents through the `SyncObserver` below); `PrSyncObserver` gains `add_observer` and a public `sync_now(project_id)` (contracts' only edit to the file); expectations, incidents and merging subscribe.
- `CAUSE_KEY` builder: `cause_key(subject: str, model: str | None) -> str` in `monitoring/incidents/contract.py`, the only place a cause key is built.
- Package `__init__.py` files: `monitoring/`, `monitoring/{results,expectations,scheduler,incidents,rules,hooks}/`, `notifications/`, `training/`, `promotion/` by contracts task 1 (S5 gives no module layout: `training/` holds fixes, requests and policies; `promotion/` holds evaluation reading, reports and promotions; the pipeline-run parts of S5 §2 extend `monitoring/hooks/` as S5 says).
- Schema (task 2), `storage/migrations/project/0005_stage4_5.sql` and `storage/migrations/app/0003_stage4_5.sql`, with `tables.py`. In `project.db`: `results` (unique source and source key), `result_sources` (id, kind, cursor JSON, last_read, last_error, consecutive_failures), `parsers` (versioned, unique source and version), `expectations` (unique kind, ref, model), `scheduler_jobs` (unique kind, due, subject), `checks`, `signals`, `incidents`, `incident_follow_ups`, `verdicts`, `misses`, `baseline_links`, `rules`, `hooks`, `hook_pins`, `hook_runs`, `agent_wakes` (agent, label, subject, key, posted_at; `AgentWaker`'s idempotency), `maintenance_windows`, `on_call_people`, `notification_log`, `monitoring_settings`, `fix_requests`, `training_requests` (unique request ID), `training_policies`, `reports`, `promotions`. Every table and column named in I22 to I38. Stage 3's `evaluations` table is not recreated. In `app.db`: the Discord and email settings and `public_url` (the integrations settings table that exists).
- Dependency `croniter`.

### I22: results (`monitoring/results/contract.py`)

```python
ResultCategory = Literal["data_quality", "drift", "model_performance", "operational"]   # retypes CheckSpec.category; the parity test in contracts task 5 compares them
ResultOrigin = Literal["team", "agent", "tracker"]    # tracker: records the app derives itself from the tracking record (ci, mlflow); S4 names two origins, see open point 1
SourceKind = Literal["file", "ci", "mlflow"]

class ResultRecord(BaseModel):          # what a check, a parser or a mapping produces
    check_id: str; origin: ResultOrigin; ran_at: datetime; fired: bool; value: float | None; threshold: float | None
    category: ResultCategory; subject: str; model: str | None; detail: dict[str, Any]
    source_ref: str; version: str | None; not_enough_data: bool = False   # version: the git commit of an agent's check; not_enough_data: a record, never a fire
class StoredResult(ResultRecord): id: str; source_id: str; source_key: str; seen_at: datetime   # res_...
class ResultQuery(BaseModel): check_id: str | None = None; model: str | None = None; origin: ResultOrigin | None = None; subject: str | None = None; fired: bool | None = None; since: datetime | None = None; until: datetime | None = None; latest: bool = False; limit: int = 50; offset: int = 0   # limit max 500
class SourceState(BaseModel): source_id: str; kind: SourceKind; cursor: dict[str, Any]; last_read: datetime | None; last_error: str | None; consecutive_failures: int
class CollectionReport(BaseModel): stored: int; by_source: dict[str, int]; failures: dict[str, str]

def source_id(kind: SourceKind, ref: str) -> str: ...   # "file:<connection>:<pattern>", "ci:<workflow>", "mlflow:<metric>"; the only builder

class ResultsObserver(Protocol):        # incidents, expectations, verdicts
    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None: ...   # new results only, after commit

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

class EvaluationCopies(Protocol):      # results task 3: the one write path of the evaluation copy into stage 3's `evaluations` table (I16 has none; I16 is untouched)
    async def store_copy(self, project_id: str, record: EvaluationRecord) -> bool: ...   # False when the tracker sync already stored (source, model, version)
```

- Cursors, per source: for a file, its size, modified time and byte offset (a file that shrank or changed before the offset is read again from the start); for `ci` and `mlflow`, the last run ID or time. `ci` maps a failed or cancelled run to a fired record; `mlflow` maps a metric point by the handoff's threshold. Both are origin `tracker`.
- Signals are raised by incidents (a `ResultsObserver`), never by results.

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
    FOLLOW_UPS = "follow_ups"; ESCALATE = "escalate"; MORNING_REPORT = "morning_report"
JOB_ORDER: Final[tuple[JobKind, ...]] = tuple(JobKind)         # the order a missed batch is run in (S4 §3); declaration order is the order
class JobDue(BaseModel): kind: JobKind; due: datetime; subject: str = ""          # subject: for example a person for the morning report; unique with kind and due

class SyncObserver(Protocol):           # the one seam for work that follows a tracking sync and a collection (I16 has no observer): scheduler task 3 runs every registered observer in the AFTER_SYNC job
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

# times (scheduler task 2)
def next_occurrences(cadence: Cadence, tz: str, after: datetime, until: datetime) -> list[datetime]: ...   # UTC instants; DST: a local time that does not exist is skipped, one that occurs twice fires once
def in_hours(hours: OnCallHours, tz: str, at: datetime) -> bool: ...                # an end before the start crosses midnight
def local_day_start(tz: str, at: datetime) -> datetime: ...
```

- Every other plan registers its job in `build_services` with `Scheduler.register`; the scheduler plan adds only `SYNC_TRACKING`, `SYNC_PRS` and `AFTER_SYNC`. Sync observers: verdicts (confirmation by labels), training (linking by the tag), pipeline-runs (the alias-history check, the pin check on the default branch), promotion (the post-deploy trigger on a moved production alias), incidents (the team's merges and deploys as suspects, from `ci_runs` and `deployments`).
- The heartbeat: `GET /api/health/heartbeat` returns `{"ok": true}` and nothing else; `HeartbeatView` also feeds the Dashboard's `heartbeat_reachable: bool` (false in local mode).

### I25: expectations and checks (`monitoring/expectations/contract.py`)

```python
class ExpectationKey(BaseModel): kind: Literal["feed", "job", "check", "training_request"]; ref: str; model: str | None = None
class ExpectationSpec(BaseModel):       # exactly one of cadence and due_at
    key: ExpectationKey; category: ResultCategory; cadence: Cadence | None; due_at: datetime | None; timezone: str
    grace_minutes: int; second_grace_minutes: int | None; activates_on_pr: int | None; source: Literal["handoff", "check", "training"]
class ExpectationState(BaseModel): key: ExpectationKey; category: ResultCategory; active: bool; next_deadline: datetime | None; last_seen: datetime | None; status: Literal["ok", "due", "late", "silent", "suspended", "pending_pr"]; since: datetime | None
class CheckView(BaseModel): id: str; check_id: str; category: ResultCategory; cadence: Cadence; timezone: str; results_location: FileResults | MlflowResults; pr_number: int; agent: str; version: str | None; status: Literal["pending_pr", "active", "retired"]; registered: datetime   # chk_...

class Maintenance(Protocol):            # incidents task 3; read by expectations and incidents
    async def suspended(self, project_id: str, scope: ExpectationKey | str, at: datetime) -> bool: ...   # a feed or job key, or a connection name
class Expectations(Protocol):           # expectations
    async def register(self, project_id: str, spec: ExpectationSpec) -> None: ...   # idempotent by key; replaces the spec
    async def retire(self, project_id: str, key: ExpectationKey) -> None: ...
    async def note_seen(self, project_id: str, key: ExpectationKey, ran_at: datetime) -> None: ...
    async def activate_for_pr(self, project_id: str, pr_number: int) -> list[ExpectationKey]: ...
    async def evaluate(self, project_id: str, now: datetime) -> list[str]: ...        # silence signal IDs raised, one per miss
    async def states(self, project_id: str) -> list[ExpectationState]: ...
```

- Expectations are seeded from `Operations.feeds` and `Operations.checks` of the current handoff on `handoff.approved`, with a `Feed`'s own cadence, grace and time zone.

### I26: signals and incidents (`monitoring/incidents/contract.py`)

```python
Severity = Literal["critical", "high", "medium", "low"]
IncidentStatus = Literal["open", "acknowledged", "mitigated", "resolved", "closed", "reopened"]   # reopened behaves as open
SignalKind = Literal["fired_result", "silence", "failed_run", "parser_failure", "source_failure", "notification_failure"]

class SignalDraft(BaseModel):
    kind: SignalKind; category: ResultCategory; subject: str; model: str | None; ran_at: datetime
    detail: dict[str, Any]; result_id: str | None; source_ref: str | None; expectation: ExpectationKey | None
class SignalView(SignalDraft): id: str; incident_id: str; seen_at: datetime; cause_key: str          # sig_...
class IncidentView(BaseModel):
    id: str; category: ResultCategory; subtype: str | None; severity: Severity; status: IncidentStatus; cause: str | None; cause_key: str; model: str | None
    signal_ids: list[str]; prs: list[int]; issues: list[int]; held: bool
    started_at: datetime; detected_at: datetime; acknowledged_at: datetime | None; mitigated_at: datetime | None; resolved_at: datetime | None; closed_at: datetime | None
    snoozed_until: datetime | None; resolved_offline: bool; reopened_count: int; verdict: "VerdictView | None"            # inc_...
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
    async def list(self, project_id: str, status: list[IncidentStatus] | None = None, limit: int = 50, offset: int = 0) -> Page[IncidentView]: ...
    async def signals(self, project_id: str, incident_id: str) -> list[SignalView]: ...
    async def acknowledge(self, project_id: str, incident_id: str, by: str) -> IncidentView: ...      # person only: the route, never a tool
    async def snooze(self, project_id: str, incident_id: str, until: datetime, by: str) -> IncidentView: ...
    async def close(self, project_id: str, incident_id: str, by: str) -> IncidentView: ...
    async def update(self, project_id: str, incident_id: str, agent: str, patch: IncidentPatch) -> IncidentView: ...   # `resolved` only when `Results.latest_per_check` for the cause key no longer fires (checked here)
    async def merge(self, project_id: str, ids: list[str], agent: str) -> IncidentView: ...
    async def split(self, project_id: str, incident_id: str, signal_ids: list[str], agent: str) -> IncidentView: ...
    async def record_hold(self, project_id: str, incident_id: str, scope: HoldScope, reason: str, by: str) -> IncidentView: ...   # the scope is what the fixes guard tests a training window against
    async def set_severity(self, project_id: str, incident_id: str, severity: Severity, reason: str, by: str) -> IncidentView: ...   # `by`: severity (code) or Alert Prioritization; the reactor re-routes
```

- Grouping is S4 §5: the same cause key within `grouping_window_minutes`, or a subject downstream of the incident's subject in the pipeline graph (`Handoffs.current(...).payload.edges` and the stage that lists the subject in `inputs` or `outputs`; a subject that matches no stage groups by cause key only, see open point 6). Incidents applies the maintenance windows (`Maintenance`) before raising anything.
- Signals from `team`-origin records are never raised (S4 §10: the baseline never opens incidents); `tracker`-origin and `agent`-origin fired records, silences, failed runs and failures of the app's own machinery are.
- A `reopened` incident is the same ID.
- The timeline is the shared event list above, filtered by `subject_id`; nothing is stored twice.

### I27: severity, routing and waking an agent (`monitoring/incidents/severity.py`, `notifications/routing.py`, `monitoring/waker.py`)

```python
class SeverityDecision(BaseModel): severity: Severity; reasons: list[str]
class SeverityPolicy(Protocol):         # severity task 2; contracts' default returns medium
    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision: ...   # deterministic code, never a model: harm now, silence of a feed or job production depends on (second deadline: critical), cost of error, scope, confidence
class Delivery(BaseModel): person: str; channel: Literal["discord", "email"]; when: Literal["now", "morning_report"]; escalation_step: int
class RoutePlan(BaseModel): deliveries: list[Delivery]; inbox: bool; open_issue: bool
class Router(Protocol):                 # severity task 3
    async def plan(self, project_id: str, incident: IncidentView, now: datetime) -> RoutePlan: ...   # critical: on-call at once at any hour, escalation after `escalation_timeout_minutes`; high: now inside the person's hours, else the morning report; medium, low: Inbox and the morning report; above the issue threshold: open_issue

WakeLabel = Literal["incident", "fix_request", "post_deploy", "follow_up"]
class AgentWaker(Protocol):             # severity task 1 (early PR); no other code posts a system message to a session
    async def wake(self, project_id: str, agent: str, label: WakeLabel, subject_id: str, text: str, key: str) -> None: ...   # a system message in the agent's session, labeled; idempotent by (agent, label, subject_id, key); outside stage 2's hop and budget limits
def diagnoser(category: ResultCategory, subtype: str | None) -> str: ...   # data_quality and drift: data-quality; model_performance: model-performance; operational: retraining for subtype failed_training, deployment for bad_deploy and failed_scoring, else orchestrator
```

- Paging never waits for a model: the reactor (an `IncidentObserver`) computes severity and sends the critical page first, and only then wakes the diagnosing agent. The message says the diagnosis follows.
- Alert Prioritization (`chat: false`) is woken once per new or changed incident; it merges (`IncidentMerge`) and may move severity one level with `AdjustSeverity`.

### I28: notifications and the morning report (`notifications/contract.py`)

```python
class DiscordSettings(BaseModel): enabled: bool; webhook_set: bool                 # the URL is in the keystore (DISCORD_WEBHOOK_REF)
class EmailSettings(BaseModel): enabled: bool; host: str; port: int; user: str; sender: str; starttls: bool; password_set: bool   # the password is in the keystore (EMAIL_PASSWORD_REF)
class Message(BaseModel): title: str; severity: Severity; cause: str | None; done: list[str]; waiting: list[str]; link: str | None; local_mode_note: bool; urgent: bool; diagnosis_pending: bool
class ChannelError(Exception): ...                                              # user-facing, scrubbed
class Channel(Protocol): name: Literal["discord", "email"]; async def send(self, target: str, message: Message) -> None: ...   # raises ChannelError
class OnCallPerson(OnCall): id: str                                              # a slug of the name, unique in the project
class OnCallPeople(Protocol):           # notifications task 3
    async def list(self, project_id: str) -> list[OnCallPerson]: ...
    async def replace(self, project_id: str, people: list[OnCall]) -> list[OnCallPerson]: ...
class Notifier(Protocol):               # notifications task 1
    async def notify(self, project_id: str, person: OnCallPerson, message: Message, *, kind: Literal["incident", "escalation", "report"], incident_id: str | None) -> Literal["sent", "retrying", "failed"]: ...   # retried and logged in `notification_log`; a channel failing repeatedly raises a `notification_failure` signal
class ReportAction(BaseModel): incident_id: str; action: str; why: str; basis: Literal["rule", "policy", "approval", "tier1"]; basis_ref: str | None
class MorningReportBody(BaseModel): person: str; since: datetime; incidents: list[str]; actions: list[ReportAction]; waiting: list[str]; suspended: list[ExpectationKey]; resolved_offline: list[str]
class MorningReports(Protocol): async def build(self, project_id: str, person_id: str, since: datetime) -> MorningReportBody: ...
```

- Links use `public_url`; without it (local mode) the message sets `local_mode_note` and carries the full summary. "Waking" is a Discord mention and an email marked urgent.

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

class HookRunner(Protocol): type: Literal["command", "ci_dispatch"]; async def run(self, project_id: str, hook: HookView, params: Mapping[str, str], authority: Authority) -> HookResult: ...
class HookLifecycle(Protocol):          # seams pipeline-runs and training use
    async def before(self, project_id: str, hook: HookView, call: HookCall) -> HookCall: ...   # may fill params or raise HookRefused
    async def after(self, project_id: str, hook: HookView, call: HookCall, result: HookResult) -> None: ...
class Hooks(Protocol):                  # hooks
    def add_runner(self, runner: HookRunner) -> None: ...
    def add_lifecycle(self, lifecycle: HookLifecycle) -> None: ...
    async def list(self, project_id: str, model: str | None = None) -> list[HookView]: ...
    async def get(self, project_id: str, hook_id: str) -> HookView | None: ...
    async def register(self, project_id: str, spec: CommandSpec, name: str, model: str | None, source: Literal["handoff", "proposal"]) -> HookView: ...   # idempotent by (model, name)
    async def set_enabled(self, project_id: str, hook_id: str, enabled: bool) -> HookView: ...
    async def suspend(self, project_id: str, hook_id: str, reason: str) -> HookView: ...
    async def approve(self, project_id: str, hook_id: str, by: str) -> HookView: ...   # person only: a route, never a tool; re-pins at the current default branch
    async def run(self, project_id: str, call: HookCall, authority: Authority) -> HookResult: ...   # after the gate: lifecycle.before, the runner, lifecycle.after; records `hook.ran` or `hook.failed` on the incident; never retries
```

- The handoff's `commands` (project level and per model) become hooks when the handoff is approved (`source: "handoff"`); the `hooks` proposal adds or changes later ones (open point 7).
- A hook with a credential reads it with `hook_ref` and the credential reaches only that hook's own container run (the command runner), never an agent's Bash.
- `Hold(incident_id, scope, reason)` records the hold on the incident and, when a hold hook is registered, runs it; with none it is a notification only, and the first audit reports the missing command as a gap.

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
class GateFacts(Protocol): async def facts(self, project_id: str, hook_id: str | None) -> tuple[bool | None, bool | None, bool | None]: ...   # (deploys verified, has production signal, team gate is authority); pipeline-runs task 5; contracts default: all None
class AuthorityRegistry: def register(self, citation: Literal["rule", "policy"], authority: ActionAuthority) -> None: ...
def decide_action(level: PermissionLevel, call: ActionCall, authority: Authority) -> PolicyDecision: ...   # agents/policy/actions.py; a second data table, the roadmap's `deploy` table in code
PROPOSE_BYPASS_OVERRIDES: Final[Mapping[str, PolicyDecision]] = {"promotion": "allow"}   # the one `propose` kind that skips its approval at bypass, after the code checks of S5 §4; every other kind asks at every level
```

The gate (contracts) consults the registered authority for the call's citation (`rule_id` or `policy_id`), then `decide_action`. Cells (the roadmap's table, written out; cells marked "?" are not in the roadmap and the lead confirms them, open point 2):

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
class SilentTeamAlert(BaseModel): result_id: str; check_id: str; subject: str; ran_at: datetime                  # a team check fired and no agent incident covers its cause key; listed for verdicts
class Verdicts(Protocol):               # verdicts
    async def record(self, project_id: str, incident_id: str, draft: VerdictDraft, by: str) -> VerdictView: ...   # a person; the incident closes only after a verdict or a person's close
    async def confirm_by_labels(self, project_id: str) -> list[VerdictView]: ...                        # model performance incidents, past the minimum sample, from Model Performance's label-based results
    async def report_miss(self, project_id: str, miss: MissReport) -> None: ...
    async def list(self, project_id: str, since: datetime | None = None) -> list[VerdictView]: ...
class Baseline(Protocol):               # verdicts task 3; never notifies and never opens an incident
    async def links(self, project_id: str, incident_id: str) -> list[BaselineLink]: ...
    async def silent_alerts(self, project_id: str) -> list[SilentTeamAlert]: ...
```

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

- Each kind implements `ProposalKind` (I19): preview, warnings, `credentials_needed` (only `hooks` can need a slot, for a hook's credential), idempotent `apply`; registered in `build_services` by the plan that owns it, with its card by that plan. The first five ask at every level; `promotion` per the override above. Proposals supersede by kind and key, as stage 3 decided.
- `rule`, `training_policy` and any `hooks` entry whose spec has `deploys: true` show that fact in their warnings.

### I33: tools and the monitoring query (`agents/tools/names.py` by contracts; each tool by the named plan)

| Tool | Class | Plan | Notes |
|---|---|---|---|
| `RegisterCheck(check_id, category, cadence, timezone, results_location, pr_number)` | read | expectations | S4 §4 |
| `IncidentUpdate(incident_id, cause?, category?, subtype?, status?, note?)` | read | incidents | S4 §5; status only `mitigated` or `resolved` |
| `IncidentMerge(ids)`, `IncidentSplit(id, signal_ids)` | read | incidents | S4 §5 |
| `IncidentFollowUp(incident_id, after_days?, after_labels?, note)` | read | incidents | not in the spec: S4 §3 and §6 need a way to register "re-check after N labels or days" (open point 3) |
| `AdjustSeverity(incident_id, direction, reason)` | read | severity | Alert Prioritization only; not in the spec (open point 3) |
| `MonitoringQuery(what, ...)` | read | incidents | below |
| `RunHook(hook_id, reason, incident_id?, fix_id?, params?, rule_id?, policy_id?)` | deploy | hooks | `action_call` gives the hook's kind and the cited rule or policy |
| `Hold(incident_id, scope, reason)` | deploy | hooks | not in the spec (open point 3); gated as kind `hold` |
| `RequestFix(incident_id, evidence)` | read | fixes | S5 §1 |
| `RecordFixChoice(fix_id, choice, why, why_not)` | read | fixes | Retraining only; not in the spec |
| `SaveReport(draft)` | read | promotion | not in the spec: the report is stored before the `promotion` proposal cites it |
| `MergePullRequest(pr_number)` | deploy | merging | S5 |

```python
QueryWhat = Literal["results", "signals", "incidents", "expectations", "checks", "rules", "hooks", "windows", "verdicts", "fixes", "training_requests", "policies", "reports", "promotions"]
class MonitoringQuery(BaseModel): what: QueryWhat; id: str | None = None; model: str | None = None; subject: str | None = None; incident_id: str | None = None; status: str | None = None; since: datetime | None = None; limit: int = 50; offset: int = 0   # limit max 200
class QueryProvider(Protocol): what: QueryWhat; async def query(self, project_id: str, q: MonitoringQuery) -> Page[Any]: ...   # each plan registers the providers of its own tables
```

- The issue threshold is stage 2's setting, read through its accessor and not stored again.
- The `MonitoringQuery` tool (incidents) dispatches to the registered providers and renders rows as text through `untrusted()`; a row's free text (a result's `detail`, a signal's detail, a note) is never part of an instruction. Agents need it because the sandbox cannot read `project.db`.

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

- Training registers a `HookLifecycle`: for hooks of kind `train` and `evaluate` it creates the `trq`, runs `Fixes.guards`, fills `{request_id}`, `{window_start}`, `{window_end}` and `{mode}`, and raises `HookRefused` with the refusals. After a started request it registers a `training_request` expectation (a tagged run by a deadline derived from the policy's run-time limit; silence raises a signal); a run whose `REQUEST_TAG` tag is seen in the tracking record links the request. A policy for a hook that deploys requires an approved rollback rule for that model, checked at apply and at every run.
- The policy `ActionAuthority` counts every run of the pipeline, the team's own included, from `Tracking.query(what="ci_runs")` for the interval and runs per week.

### I36: running the team's pipeline (`monitoring/hooks/`, pipeline-runs)

```python
class RunContext(BaseModel): request_id: str; model: str; champion_version: str | None; window: TrainingWindow | None; mode: Literal["train", "evaluate"] | None; candidate_version: str | None; threshold: float | None
class MissingPlaceholder(HookRefused): placeholder: str
class PlaceholderResolver(Protocol):    # pipeline-runs task 1 (early PR)
    async def resolve(self, hook: HookView, ctx: RunContext) -> dict[str, str]: ...   # resolved at the moment of the run; `champion_version` read from the tracker then; a placeholder the hook declares and the context lacks raises MissingPlaceholder before running
class DispatchRef(BaseModel): workflow: str; run_url: str | None
class CiDispatcher(Protocol): async def dispatch(self, project_id: str, workflow: str, inputs: dict[str, str]) -> DispatchRef: ...   # always the default branch, through the app's GitHub client
class PinCheck(BaseModel): ok: bool; changed: list[str]
class AliasCheck(BaseModel): moved: bool; detail: str
class HookVerifier(Protocol):
    async def verify_pins(self, project_id: str, hook_id: str) -> PinCheck: ...        # hashes of the workflow file and the declared entry files on the default branch against `HookView.pins`
    async def check_run(self, project_id: str, hook_id: str, request_id: str, window: TrainingWindow) -> AliasCheck: ...   # the tracker's alias history over the run's window
```

- A `deploys: false` run that moved a production alias suspends the hook and every policy using it, raises a high-severity incident, and treats the hook's runs as deploy actions until the person approves again (`Hooks.approve`). A changed pin does the same. A hook whose model has no production signal never runs under a policy: `GateFacts` reports `has_production_signal = false`, so the gate asks.
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
def comparable(a: EvaluationPayload, b: EvaluationPayload) -> bool: ...   # same evaluation_commit and gate_config_version

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

- `person` mode: Retraining proposes with `Propose(kind="promotion")`; at approval `validate` runs again, then the model's deploy hook runs (basis `approval`); at `bypass` the same checks run and the apply needs no click. `team_gate` mode: the app never deploys; it reads the gate's decision and reasons from the evaluation record, notifies the person, and stage 4 watches the new version.
- The post-deploy comparison: when tracking sees a production alias move (`DeploymentRow`), promotion wakes Deployment & Rollback (`post_deploy`) with the model and versions; the agent compares the record and the live label-based results.

### I38: merging (`integrations/github/merge.py`, `agents/tools/merge.py`)

```python
class ProtectionSurfaces(Protocol):     # merging task 2
    async def paths(self, project_id: str) -> list[str]: ...                    # entry files of the model's evaluate and deploy hooks, the workflow files behind the branch protection's required checks (branch protection API), CODEOWNERS; open point 5
    async def touches(self, project_id: str, pr_number: int) -> list[str]: ...   # the surface paths the PR changes; the agent's labels are never read
class MergeCheck(BaseModel): ok: bool; refusals: list[str]       # own PR, mergeable and clean, no admin override, at least one passing check, no protection surface
```

- `MergePullRequest` is in code: an agent merges only its own PR; the platform reports it mergeable and clean; at least one check passed (a repository without CI never self-merges, nor does a local review); never an admin override; never a PR that touches a protection surface, at any level. Merges write `pr.merged_by_agent`.

### I39: the Dashboard's data (`GET /api/projects/{id}/dashboard`, dashboard task 1)

```python
class StageStatus(BaseModel): name: str; status: Literal["ok", "late", "incident", "unknown"]; incident_ids: list[str]
class ModelCard(BaseModel): name: str; production_version: str | None; latest_metrics: dict[str, float]; open_incidents: int
class AgentActivity(BaseModel): agent: str; state: AgentState; doing: str | None            # stage 2's AgentStatus fields
class DashboardView(BaseModel): stages: list[StageStatus]; edges: list[PipelineEdge]; models: list[ModelCard]; incidents_by_severity: dict[Severity, int]; at_risk: list[ExpectationState]; agents: list[AgentActivity]; heartbeat_reachable: bool; baseline_available: bool
```

### I40: the Deployments page's data (`GET /api/projects/{id}/deployments`, deployments task 1)

```python
class EnvironmentRow(BaseModel): model: str; environment: str; alias: str; version: str | None
class DecisionRow(BaseModel): at: datetime; model: str; kind: Literal["promotion", "rollback", "fall_back"]; from_version: str | None; to_version: str | None; decided_by: Literal["person", "team_gate", "rule", "agent_bypass"]; decided_by_ref: str | None; report_id: str | None   # decided_by_ref: the rule ID or the person
class OpenPipelinePr(BaseModel): number: int; title: str; agent: str; labels: list[str]
class DeploymentsView(BaseModel): environments: list[EnvironmentRow]; decisions: list[DecisionRow]; open_prs: list[OpenPipelinePr]
```

- Environments come from tracker aliases and the handoff's `ModelSpec.environments`; decisions from `Promotions.history`, `hook_runs` of kind rollback and fall_back (with their rule), and tracking's deployments.

## Spec coverage

Every section of both specs, the numbered changes and the roadmap bullets, with the plan that builds it (c = contracts, "all" = each plan for its own module).

| Spec | Plan |
|---|---|
| S4 §1 layout | c (packages); each plan its module |
| S4 §2 record, sources, parsers, cursors, times, untrusted text | results; times: scheduler; untrusted: c (`untrusted`), incidents (`MonitoringQuery`), definitions (prompts) |
| S4 §2 catching up (reading, "resolved while offline") | results (oldest first), scheduler (job order), incidents task 5 |
| S4 §3 the app's scheduler and jobs | scheduler; each job by its owner (collect: results; deadlines: expectations; follow-ups: incidents; escalate: severity; report: notifications) |
| S4 §3b heartbeat route, the job in the team's scheduler | scheduler (route); definitions (Deployment & Rollback delivers the job by PR; Onboarding proposes it); dashboard (local-mode notice) |
| S4 §3a expectations and silence | expectations; maintenance: incidents |
| S4 §4 checks by agents, `RegisterCheck`, minimum sample | expectations (`RegisterCheck`); results (records); definitions (check content: Data Quality, Model Performance) |
| S4 §5 signals, grouping, incident, lifecycle, quiet period, windows, snooze, tools | incidents |
| S4 §6 diagnosis: waking, the menu | severity (waking), fixes (menu 6), hooks (menu 3 to 5), definitions (menu, revert own PR, fix causes through PRs) |
| S4 §6 hooks, credentials, hold | hooks |
| S4 §6 rules | rules |
| S4 §6 post-deploy comparison, humans decide deploys | promotion (trigger), definitions (comparison), c and hooks (gate) |
| S4 §6 recent changes are suspects | incidents task 5; revert: definitions; merging (PR observer) |
| S4 §7 severity, routing, escalation, Alert Prioritization | severity |
| S4 §8 channels, links, on-call, report, failures | notifications; Integrations UI: settings |
| S4 §9 proposal kinds | c (payloads, names); apply: results (`parser`), rules (`rule`), hooks (`hooks`), incidents (`maintenance`) |
| S4 §10 verdicts, labels, baseline | verdicts; buttons and the miss report: dashboard |
| S4 §10 Knowledge note per resolved incident (`project_knowledge.md`) | definitions (agents `SaveNote` on resolution); open point 8 |
| S4 §11 UI | dashboard, settings |
| S4 §12 storage, changes to the roadmap | c (schema, IDs, gate); heartbeat exception: scheduler |
| S4 §13 errors | results (sources, parsers), hooks (hook failure), notifications (retry), all (scrubbing) |
| S4 §14 testing, Done when | each plan its tests; the night and the end-to-end: definitions task 3 |
| S5 hard limits | existing limits stay stage 1 and 2's; new in code: merging, pipeline-runs (default branch only, pins), c (gate) |
| S5 `MergePullRequest` | merging; class and cells: c |
| S5 the principle (PRs, continuously; PR discipline) | definitions (prompts); merging (closed-unmerged note, merge events); open point 9 |
| S5 stage responsibilities, change 5 | fixes, definitions |
| S5 change 1 shadow evaluation, change 2 canary | definitions (agents add them through PRs); promotion (the record carries the result) |
| S5 change 3 bypass | c (gate cells, override) |
| S5 change 4 running the pipeline by what it does | c (cells), pipeline-runs (`GateFacts`), training (policy authority) |
| S5 change 6 several models | c (every record keyed by `model`), dashboard (a list), expectations, severity; tests use one |
| S5 §1 fix requests and menu, guards | fixes; breaker: fixes task 3 |
| S5 §2 hooks: placeholders, `ci_dispatch`, `deploys` verified | pipeline-runs |
| S5 §2 policy, circuit breaker, linking back, reproducibility | training (policy, linking), fixes (breaker), definitions (reproducibility PRs) |
| S5 §3 the evaluation record, comparability, authoritative number | promotion; the step itself in the team's pipeline: definitions |
| S5 §4 promotion modes, the app's report | promotion; the card: deployments |
| S5 §5 agents' instructions | definitions |
| S5 §6 UI | deployments |
| S5 §7 storage, roadmap changes | c; kinds `training_policy` and `promotion`: training, promotion |
| S5 §8 testing, Done when | each plan its tests; the end to end: definitions task 4; fake MLflow and GitHub additions: pipeline-runs, promotion, merging |

## Open points for the lead

Resolved in this draft as stated; each needs the lead's confirmation or a Decision issue before the plan that depends on it starts.

1. Origin of ci and mlflow records. S4 §2 names `team` and `agent`; S4 §10 says `team` records never open incidents, yet S4 §5 raises signals for failed jobs and runs. This overview adds `tracker` for records the app derives from the tracking record, which raise signals; `team`-origin file records only feed the baseline; a team job's silence still raises through expectations.
2. Gate cells with no roadmap value: `train`, `evaluate` and a deploying run at `suggest`, and their no-authority cells at `bypass`; `hold` at `suggest` ("applied by the person in one click" is modeled as `ask`). S5's "Changes to the roadmap" says promotion in `person` mode asks at `bypass`; S5 change 3, S5 §4 and the roadmap table say it needs no approval at `bypass`. This draft follows the roadmap (a per-kind `propose` override for `promotion`) and treats that bullet as stale.
3. Tools the specs need but do not name: `IncidentFollowUp`, `AdjustSeverity`, `Hold`, `RecordFixChoice`, `SaveReport`, `MonitoringQuery`, and the name `RunHook`.
4. Which feeds "production depends on" (severity, S4 §7): `Feed` has no flag. Severity derives it from the pipeline graph (a feed whose stage has a path to a model's stage); a flag in the handoff would be a stage 3 change.
5. Protection surfaces (S5): the handoff has no field listing the promotion gate or the evaluation decision-logic files. This draft derives them from the `entry_files` of each model's evaluate and deploy hooks, the required-check workflows from the branch protection API, and `CODEOWNERS`.
6. Mapping a signal's subject (a table, column, model version, job) onto a pipeline stage: `PipelineStage.inputs` and `outputs` are strings; incidents matches them to the subject by exact name, else groups by cause key only.
7. Hook registration has two sources: the approved handoff's `commands`, and the `hooks` proposal kind. This draft registers the handoff's at `handoff.approved` and uses the proposal for later ones. Approving changed hook content after a suspension is a person-only route (`Hooks.approve`), not an agent tool.
8. Who writes the Knowledge note per resolved incident: the diagnosing agent (instructed by its definition), not the app.
9. Whether the PR discipline's open-PR cap and `decision-semantics` label are enforced in code or only in the prompts. The specs enforce only the merge rules in code; this draft enforces the closed-unmerged note trigger only.
10. A system wake of an agent (`AgentWaker`) is outside stage 2's hop and budget limits, so a bad night is not capped; stage 2's caps stay for agent-to-agent messages.
11. The report in `team_gate` mode is "written to the run" (S5 §4), but the app has no tracker write path; this draft stores it as a `rep` record and shows it, and the team's pipeline writes its own.
12. Milestones: the contracts plan spans both stages. This draft proposes the stage 4 milestone for it and for the stage 4 plans, and the stage 5 milestone for the stage 5 plans.
13. The shared working rules (stage 3 onboarding adds the hard limits and the continuous-PR principle to `context.py` from packaged files) and S5 §5's "all seven definitions state it": the definitions plan references the packaged text rather than repeating it.

## Files shared by these plans

Each file below is edited by one plan only, except where a line says otherwise:
- `storage/migrations/project/0005_stage4_5.sql`, `storage/migrations/app/0003_stage4_5.sql`, `storage/tables.py`, `ids.py`, `config.py` (`croniter`), `credentials/project_secrets.py` (`hook_ref`), `events/contract.py` (`EventType`), the frontend `PROJECT_EVENT_TYPES`, `agents/policy/table.py` and the new `agents/policy/actions.py`, `agents/approvals/gate.py`, `agents/approvals/authority.py`, `agents/runtime/hooks.py` (`GateCall.action`), `agents/tools/base.py` (`action_call`), `agents/tools/names.py`, `agents/pr_sync.py` (`PrObserver`, `sync_now`), `monitoring/settings.py`, `monitoring/jobs.py`, `monitoring/untrusted.py`, `monitoring/expr.py`: contracts. A later plan that needs a change opens a Decision.
- Every `contract.py` and types module named in I22 to I38 belongs to contracts (task 1; I23's `expr.py` in task 4); the owners implement in sibling modules and never edit them. Below, a plan's directory means those sibling modules.
- `monitoring/results/*`: results. `monitoring/scheduler/*`: scheduler. `monitoring/expectations/*`: expectations. `monitoring/incidents/*`: incidents, except `monitoring/incidents/severity.py` and `monitoring/waker.py`: severity. `monitoring/rules/*`: rules. `monitoring/verdicts/*`: verdicts. `notifications/*`: notifications, except `notifications/routing.py`: severity.
- `monitoring/hooks/*`: hooks, except `placeholders.py`, `ci_dispatch.py`, `pins.py`, `alias_check.py`, `facts.py`: pipeline-runs (pipeline-runs registers through `Hooks.add_runner`, `add_lifecycle` and the gate's `GateFacts`, and edits no hooks file).
- `training/*`: fixes owns `training/fixes/`, `training/guards.py` and `training/circuit.py`; training owns `training/policies/` and `training/requests/`. `promotion/*`: promotion. `integrations/github/dispatch.py`: pipeline-runs; `integrations/github/merge.py`, `agents/tools/merge.py`: merging; neither edits `integrations/github/api.py`.
- `sandbox/containers.py` (stage 3 connections' file): hooks adds only the hook credential's injection into one container run; results' code parser uses the sandbox's existing run API with the network off and a time limit and edits nothing there; a missing option opens a Decision.
- `agents/tools/__init__.py` (`default_registry`) and `api/services.py`: each plan adds only its own lines; each plan that registers a proposal kind, a job, a runner, a lifecycle, an authority or a query provider does it in `build_services`, after the registries exist.
- `api/security.py` and `api/hosts.py`: scheduler (the heartbeat exception and the `public_url` host rule, only its lines).
- `tests/fakes/fake_smtp.py`, `tests/fakes/fake_discord.py`: notifications. `tests/fixtures/results/`: results. `tests/fakes/fake_github.py` (stage 3's): pipeline-runs (workflow dispatch, run status) and merging (PR checks, mergeable state, merge, branch protection, file contents), each in its own functions. `tests/fakes/fake_mlflow.py` (stage 3's): pipeline-runs (run tags, model versions, alias moves) and promotion (run artifacts), each in its own functions.
- `agents/definitions/*.md`: definitions only, for stages 4 and 5 (stage 3 onboarding edited them first).
- `frontend/src/pages/agents/ApprovalCard.tsx`: each proposal-kind owner adds its own card component file and one line in the dispatcher: results (`parser`), rules (`rule`), hooks (`hooks`), incidents (`maintenance`), training (`training_policy`), deployments (`promotion`).
- `frontend/src/pages/project-settings/ProjectSettingsPage.tsx` and `frontend/src/pages/integrations/*`: settings, one line per section; the section components live in its own files and read the routes of the owning plans.
- `frontend/src/pages/inbox/InboxPage.tsx`, `frontend/src/pages/project-home/*`: dashboard (the Dashboard replaces the project home). New pages carry their own `route.tsx` and nav entry, so the route table and `Sidebar.tsx` need no edit.
- `backend/tests/e2e/test_monitoring_night.py`, `backend/tests/e2e/test_fix_and_promotion.py`, `frontend/e2e/monitoring.spec.ts`, `frontend/e2e/actions.spec.ts`, and the fixture project for them: definitions only.
- `frontend/openapi.json`: the plan that changes the API regenerates it; contracts adds none (no routes), scheduler adds the heartbeat route.
