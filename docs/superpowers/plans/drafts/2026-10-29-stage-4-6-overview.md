# Stages 4 to 6: Monitoring, Action Agents and Stats — Plans Overview

> **Draft.** Missing: the lead's answers to the open points (last section); then the plan files, each written short (AGENTS.md "How plans are written", bar of 2026-10-29).

Stages 4, 5 and 6 are built through the ten plans below, with one contracts plan for all three. This overview holds what they share. Read it, `docs/roadmap.md`, `project_knowledge.md` (Process, 2026-10-29), the stage 3 overview (its interfaces I13 to I21 are frozen inputs) and both specs before starting any plan.

**Goal:** the app watches the team's production ML system at any hour, turns signals into a few incidents, has the right agent diagnose each one, acts within the three tiers, tells the right person at the right time, and, when the model needs a fix, requests it from Retraining, which runs the team's own pipeline; evaluation, promotion and merging stay the team's, made rigorous through agents' PRs. Stats shows how the agents do against the team's own monitoring, and Activity, Knowledge and Usage show what was collected.

**Specs:** `docs/superpowers/specs/2026-10-28-stage-4-monitoring-design.md` and `docs/superpowers/specs/2026-10-28-stage-5-action-agents-design.md` (both approved 2026-10-28); "S4" and "S5" name which, §n is the section. Stage 6 has no spec: the roadmap's stage 6 bullets, the 2026-10-29 decisions in `project_knowledge.md` (5 verdicts before a number shows; the price source) and I41 and I42 below are its spec (open point 17).

**Tech stack:** as in stage 3, plus `croniter` (cron cadences; one version constant in `config.py`). No other new dependency.

## Global constraints

- `AGENTS.md` applies, with the bar of 2026-10-29: tests cover the main path and the failures that really happen; accepted shortcuts go under "Known debt added" and into `docs/known-debt.md`. Never relaxed: secrets, the hard limits, no data loss, no stub features.
- The specs' principles: the app is an extension of the team's system and never runs a check, a model, a training or a deploy of its own; text read from the team's data, results, logs and parsers is data, never instructions, and reaches an agent only through `untrusted(text)` (I33); the gate and the rules are evaluated in code.
- Secrets never go in a proposal, a log, an event, a checkpoint or a message; a Discord webhook URL is a secret (keystore and scrubber).
- External services are faked in tests: result sources from fixture folders (`backend/tests/fixtures/results/`), a fake SMTP server and a fake Discord webhook (`tests/fakes/`), the fake clock (`Clock`, I21), stage 3's fake MLflow and GitHub servers (extended below).
- One source for every constant (job order, setting defaults, severity and status literals, hook kinds, tool names, proposal kind names, required report fields, the request tag, wake labels, event types, the Stats metric names). A literal that restates a stage 3 one (`ResultCategory`, `ActionKind`) is retyped once and parity-tested (contracts task 5).
- Frontend types only from the generated `schema.d.ts`; a plan that changes the API commits the regenerated `frontend/openapi.json`. Routers are discovered by `include_routers`; a plan adds its router file and no registration line.

## Plans

Ten plans, one area each. Every plan builds its part of the core path first and its extras last, so the deadline can only cut tails: **core path** = results read → incident → silence → notify → diagnose → fix → promote → verdicts → Stats. **Extras** (each plan's last tasks): rules, training policies and the pin and alias checks they need, agents merging their own PRs, maintenance windows and snooze, escalation, the morning report, parsers in code, the full settings sections. The request was nine plans; notifications is its own plan (ten) because it sits on the core path between incidents and severity routing, and one owner holding incidents, severity and notifications in sequence would be the bottleneck of the whole stage.

The interfaces below keep the areas of the earlier draft in their comments (`# severity`, `# pipeline-runs`, ...); this table says which plan owns each area.

| Plan | Branch | Areas (interfaces) | Consumes (besides merged code) | Starts | Milestone |
|---|---|---|---|---|---|
| contracts | `plan/stage-4-6-contracts` | the types of I22 to I42, both migrations, the gate seam (I30), `expr` and the six payloads (I32), consistency tests | stage 3 I13 to I21 (on `main`); task 4 also the stage 3 proposals plan merged (open point 16) | tasks 1 to 3 now; task 4 after stage 3 proposals merges | Stage 4 |
| results | `plan/stage-4-results` | results, scheduler, expectations (I22, I24, I25), parsers, `RegisterCheck` | contracts tasks 1, 2 (task 4 for the `parser` kind); I16 (merged) | after contracts tasks 1 and 2 | Stage 4 |
| incidents | `plan/stage-4-incidents` | incidents, severity (I26, I27), the incident tools and `MonitoringQuery`, Alert Prioritization's run, maintenance | contracts tasks 1, 2 (task 4 for the `maintenance` kind); results task 2 (`ResultsObserver`); notifications tasks 1, 3 (for task 4) | after contracts tasks 1 and 2 | Stage 4 |
| notifications | `plan/stage-4-notifications` | notifications (I28): channels, Integrations routes, on-call, escalation, morning report | contracts tasks 1, 2; incidents task 1 (task 4 on) | after contracts tasks 1 and 2 | Stage 4 |
| hooks | `plan/stage-4-hooks` | hooks (I29), `RunHook`, `Hold`; rules (I23's rules) last | contracts tasks 1 to 3 (task 4 for its kinds); incidents task 1; I20 `Handoffs` (merged) | after contracts tasks 1 to 3 | Stage 4 |
| training | `plan/stage-5-training` | fixes (I34), pipeline-runs (I36), training (I35): `RequestFix` → Retraining runs the team's pipeline first; policies, pins and the alias check last | contracts tasks 1 to 3 (task 4 for the policy kind); incidents tasks 1, 2; hooks task 1; results task 4 (`SyncObserver`) | after incidents task 2 and hooks task 1 | Stage 5 |
| promotion | `plan/stage-5-promotion` | promotion (I37) in `person` mode first, then `team_gate`; the Deployments data (I40); merging (I38) last | contracts tasks 1 to 4; hooks task 3; training task 3; results task 4; stage 3 tracking (merged) | task 1 after contracts tasks 1 and 2; task 3 after hooks task 3 and contracts task 4 (so stage 5's person-mode promotion waits on the stage 3 proposals plan; the stage 5 end-to-end test already waits on it through onboarding) | Stage 5 |
| stats | `plan/stage-6-stats` | verdicts and the baseline (I31), Stats (I41) and its page | contracts tasks 1, 2; incidents task 1; results task 2 | after incidents task 1 | Stage 6 |
| ui | `plan/stage-4-6-ui` | the Dashboard (I39), Inbox incidents, the incident page and verdict buttons, the settings sections, the Deployments page, Activity, Knowledge, Usage (I42) | the routes of the plans above; stage 3 knowledge (#406) for the Knowledge page | task 1 after incidents task 1 and results task 5; each later task when its routes are on `main` | Stage 4 |
| definitions | `plan/stage-4-6-definitions` | the seven definitions for stages 4 and 5; the end-to-end tests (the simulated night; fix to promotion) | every plan above; stage 3 onboarding (it edits the definitions first) | after stage 3 onboarding, and per test when its plans are on `main` | Stage 4 |

**Task outlines** (each plan file writes them out, short; core path first; "early PR" = another plan consumes it before this plan ends):
- **contracts:** 1 the shared module of I22 to I42 (types only) (early PR); 2 the migrations and `tables.py` (early PR); 3 the gate seam I30 with its defaults (early PR); 4 `monitoring/expr.py`, the six payloads, and I19's `ProposalView.payload` widened to a union discriminated by kind (early PR; starts after the stage 3 proposals plan has merged, open point 16); 5 consistency tests and the regenerated `openapi.json` if a type reaches the API.
- **results:** 1 the scheduler loop, job table, `JOB_ORDER`, catching up, times and DST (early PR); 2 the record, `Results` store, idempotent ingest, query, the `file` source and cursors (early PR); 3 the `ci` and `mlflow` sources, `collect_results`, the evaluation copy; 4 `sync_tracking`, `sync_prs`, `after_sync`, `SyncObserver`, the heartbeat route (early PR); 5 expectations: seeded from the current handoff at startup and through `HandoffObserver` (open point 21; tests call `Handoffs.store` directly, so this does not wait for stage 3 onboarding), deadlines, grace, silence signals, `check_expectations` (early PR); 6 parsers (mapping and code) and the `parser` kind and card; 7 `RegisterCheck`, the `checks` store, activation on the PR's merge.
- **incidents:** 1 signals, incidents, `raise_signal`, lifecycle, observers, timeline events (early PR); 2 `AgentWaker` and the diagnosing-agent dispatch (early PR); 3 grouping (cause key, window, downstream), quiet period, reopen, recent-change suspects, "resolved while offline"; 4 `SeverityPolicy`, `Router`, the reactor that pages at once, the issue threshold; 5 the incident tools, `MonitoringQuery` and the `follow_ups` job; 6 Alert Prioritization's run and `AdjustSeverity`; 7 maintenance windows, snooze, the `maintenance` kind and card, `Maintenance` (until then the contracts default: never suspended).
- **notifications:** 1 channels, `Notifier`, retry, `notification_log`, the fake SMTP and Discord servers (early PR); 2 the Integrations routes: Discord and email with Test, keystore refs, `public_url`, links, the local-mode note; 3 `OnCallPeople`, routes, seeded from the current handoff at startup and through `HandoffObserver` (open point 21; tests call `Handoffs.store` directly, so this does not wait for stage 3 onboarding) (early PR); 4 escalation and the severity-rise re-notification; 5 the morning report.
- **hooks:** 1 the `Hooks` store, registration of the handoff's commands seeded from the current handoff at startup and through `HandoffObserver` (open point 21; tests call `Handoffs.store` directly, so this does not wait for stage 3 onboarding), the runner and lifecycle seams (early PR); 2 hook credentials (route, `hook_ref`, injection into one container run) and the `command` runner; 3 `RunHook`, `Hold`, failures recorded on the incident (early PR); 4 the `hooks` kind and card (after contracts task 4); 5 the `Rules` store, the `rule` kind, routes and card; 6 the evaluator and limits, the rule `ActionAuthority`, the timeline record.
- **training:** 1 `RequestFix`, `RecordFixChoice`, the store, the wake of Retraining (early PR); 2 the guards (early PR); 3 `PlaceholderResolver` and the training `HookLifecycle` (the `trq`, `{request_id}`, the window) (early PR); 4 `CiDispatcher` and the `ci_dispatch` runner (the fake GitHub's dispatch); 5 linking back by the request tag and the `training_request` expectation; 6 the circuit breaker, decline back to observe, the outcome of each fix; 7 pins, hashes, suspension, the person's approve route, the alias-history check, `GateFacts`; 8 the `training_policy` kind, store, routes, card and the policy `ActionAuthority`.
- **promotion:** 1 `EvaluationReader`, `comparable`, "not computed" (early PR); 2 `Reports`, required fields, `SaveReport` (early PR); 3 the `promotion` kind and card, `person` mode, re-validation, the deploy hook as apply; 4 the post-deploy trigger (wake Deployment & Rollback); 5 `team_gate` mode; 6 `GET .../deployments` (I40); 7 `MergePullRequest`, its code checks and `ProtectionSurfaces`, the closed-unmerged observer and merge events.
- **stats:** 1 verdicts and miss reports, routes (early PR); 2 confirmation by labels; 3 the baseline record and the silent-alerts list; 4 the Stats computation and route (I41); 5 the Stats page.
- **ui:** 1 `GET .../dashboard` (I39) and the Dashboard page; 2 Inbox incidents (rank, acknowledge, snooze), the incident page with its timeline, verdict buttons and the miss report; 3 Integrations (Discord, email, `public_url`) and the project settings for on-call, intervals, the issue threshold, the escalation timeout, the time zone; 4 the Activity page; 5 the Knowledge page; 6 Usage (I42): the route, the price on the model, the page; 7 the Deployments page; 8 the settings sections for hooks (credential, enable, approve changed content), checks and schedules, rules, policies and maintenance windows.
- **definitions:** 1 Data Quality, Model Performance, Alert Prioritization; 2 Retraining, Deployment & Rollback, Orchestrator, Onboarding; 3 the stage 4 end-to-end test (the simulated night); 4 the stage 5 end-to-end test (incident to fix to promotion).

Owners claim plans as `AGENTS.md` says; contracts first, since every other plan starts on its early PRs. Ten plans for six people: an owner whose next task waits on another plan claims a second one meanwhile. Suggested waves: contracts; then results, incidents, notifications, hooks; then training, promotion, stats; then ui; then definitions.

## Interfaces between plans

contracts writes these into code exactly as here (except where a line names another producer); changing one takes a Decision issue. Fields are pydantic models unless marked Protocol; every payload model is `extra="forbid"`. `Page` is I16's, `Clock` is I21's (rate-limits task 1: `providers/failover.py`; `clock.py` has only `now_iso`), `Cadence`, `Feed`, `OnCall`, `CheckSpec`, `CommandSpec`, `FileResults`, `MlflowResults`, `Profile`, `ModelSpec`, `PipelineEdge` are I19's. Changelog: none yet.

### Shared by contracts (task 1)

- ID prefixes `res`, `sig`, `inc`, `rul`, `hok`, `win`, `chk`, `fix`, `trq`, `pol`, `rep`, `prm` in `ids.py` (`inc` is in the roadmap but not yet in `ids.py`).
- Event types, in the backend `EventType` (`events/contract.py`) and the frontend `PROJECT_EVENT_TYPES`:
  - stage 4: `results.collected`, `signal.raised`, `incident.opened`, `incident.updated` (payload: the changed fields), `incident.acknowledged`, `incident.snoozed`, `incident.severity_changed`, `incident.reopened`, `incident.merged`, `incident.split`, `incident.suspect_linked`, `incident.hold_recorded`, `incident.wake_skipped`, `incident.closed`, `check.registered`, `parser.disabled`, `notification.sent`, `notification.failed`, `escalation.sent`, `report.sent`, `hook.ran`, `hook.failed`, `hook.suspended`, `rule.satisfied`, `verdict.recorded`, `miss.reported`;
  - stage 5: `fix.requested`, `fix.chosen`, `fix.declined`, `fix.escalated`, `training.requested`, `training.linked`, `policy.suspended`, `report.saved`, `promotion.applied`, `promotion.refused`, `pr.merged_by_agent`, `evaluation.stored`.
  - A timeline entry of an incident is an event whose `subject_id` is the incident ID; actions on hooks, rules and notifications carry the incident as `subject_id` when there is one, so every entry is recorded once.
- Constants in one module per concern, each with a single definition: `monitoring/settings.py` (`MonitoringSettings`, below), `monitoring/jobs.py` (`JOB_ORDER`, I24), `agents/tools/names.py` (every tool name of I33), `monitoring/untrusted.py` (`untrusted(text: str) -> str`, which wraps text in the marker block the agents' prompts name), `training/constants.py` (`REQUEST_TAG = "mlops_agents.request_id"`, the tag the team's pipeline writes on its MLflow run; `EVALUATION_ARTIFACT = "mlops_agents/evaluation.json"` is stage 3's), `promotion/constants.py` (`REQUIRED_REPORT_FIELDS`, I37).
- `MonitoringSettings` (project settings, defaults in one place, stored in `monitoring_settings`): `collection_interval_s=60`, `grouping_window_minutes`, `reopen_window_hours`, `recent_change_window_hours`, `escalation_timeout_minutes`, `parser_disable_after`, `evaluation_max_age_hours`, `circuit_breaker_fixes`, `circuit_breaker_window_days`, `timezone: str | None` (None = the server's). App settings (`app.db`): `public_url: str | None`, `DiscordSettings`, `EmailSettings` (I28).
- `credentials/project_secrets.py` gains the one builder `hook_ref(project_id, hook_id)` (ref `project/<id>/hook/<hok>`, under `project_prefix`, so project removal deletes it) and `hook_prefix`; the app-level refs `DISCORD_WEBHOOK_REF` and `EMAIL_PASSWORD_REF` are constants beside the GitHub token's. No other code writes a ref string.
- `PrObserver` (Protocol, `agents/pr_sync.py`): `async changed(project_id, pr: PrChange)`, with `PrChange(number: int, state: Literal["open","merged","closed"], merged_at: datetime | None, head_branch: str, agent: str, files: list[str])` (stage 2's sync sees only agents' task PRs, so `agent` is always set; the team's own merges and deploys reach incidents through the `SyncObserver` below); `PrSyncObserver` gains `add_observer` and a public `sync_now(project_id)` (contracts' only edit to the file); expectations, incidents and merging subscribe.
- `HandoffObserver` (Protocol, `proposals/contract.py`, an I20 change, open point 21): `async stored(project_id: str, handoff: HandoffView) -> None`, called by `Handoffs.store` after commit; `Handoffs` gains `add_observer(observer)` (contracts' only edit to `proposals/handoffs.py`). Every observer also reconciles `Handoffs.current()` once per project at startup, since projects onboarded before stage 4 never fired it. Results (expectations), notifications (on-call) and hooks (registration) subscribe; nothing else reacts to a stored handoff.
- `CAUSE_KEY` builder: `cause_key(subject: str, model: str | None) -> str` in `monitoring/incidents/contract.py`, the only place a cause key is built.
- Package `__init__.py` files: `monitoring/`, `monitoring/{results,expectations,scheduler,incidents,rules,hooks,verdicts}/`, `notifications/`, `training/`, `promotion/`, `stats/` by contracts task 1 (S5 gives no module layout: `training/` holds fixes, requests and policies; `promotion/` holds evaluation reading, reports and promotions; the pipeline-run parts of S5 §2 extend `monitoring/hooks/` as S5 says).
- Schema (task 2), `storage/migrations/project/0007_stage4_5.sql` and `storage/migrations/app/0003_stage4_5.sql`, with `tables.py`. In `project.db`: `results` (unique source and source key), `result_sources` (id, kind, cursor JSON, last_read, last_error, consecutive_failures), `parsers` (versioned, unique source and version), `expectations` (unique kind, ref, model), `scheduler_jobs` (unique kind, due, subject), `checks`, `signals`, `incidents`, `incident_follow_ups`, `verdicts`, `misses`, `baseline_links`, `rules`, `hooks`, `hook_pins`, `hook_runs`, `agent_wakes` (agent, label, subject, key, posted_at; `AgentWaker`'s idempotency), `maintenance_windows`, `on_call_people`, `notification_log`, `monitoring_settings`, `fix_requests`, `training_requests` (unique request ID), `training_policies`, `reports`, `promotions`. Every table and column named in I22 to I38. Stage 3's `evaluations` table is not recreated. In `app.db`: the Discord and email settings and `public_url` (the integrations settings table that exists), and the price columns on `models` (I42).
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

class SyncObserver(Protocol):           # the one seam for work that follows a tracking sync and a collection (I16 has no observer): results task 4 runs every registered observer in the AFTER_SYNC job
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
def next_occurrences(cadence: Cadence, tz: str, after: datetime, until: datetime) -> list[datetime]: ...   # UTC instants; DST: a local time that does not exist is skipped, one that occurs twice fires once
def in_hours(hours: OnCallHours, tz: str, at: datetime) -> bool: ...                # an end before the start crosses midnight
def local_day_start(tz: str, at: datetime) -> datetime: ...
```

- Every other plan registers its job in `build_services` with `Scheduler.register`; the results plan's scheduler adds only `SYNC_TRACKING`, `SYNC_PRS` and `AFTER_SYNC`. Sync observers: verdicts (confirmation by labels), training (linking by the tag), pipeline-runs (the alias-history check, the pin check on the default branch), promotion (the post-deploy trigger on a moved production alias), incidents (the team's merges and deploys as suspects, from `ci_runs` and `deployments`).
- The heartbeat: `GET /api/health/heartbeat` returns `{"ok": true}` and nothing else; `HeartbeatView` also feeds the Dashboard's `heartbeat_reachable: bool` (false in local mode).

### I25: expectations and checks (`monitoring/expectations/contract.py`)

```python
class ExpectationKey(BaseModel): kind: Literal["feed", "job", "check", "training_request"]; ref: str; model: str | None = None
class ExpectationSpec(BaseModel):       # exactly one of cadence and due_at
    key: ExpectationKey; category: ResultCategory; cadence: Cadence | None; due_at: datetime | None; timezone: str
    grace_minutes: int; second_grace_minutes: int | None; activates_on_pr: int | None; source: Literal["handoff", "check", "training"]
class ExpectationState(BaseModel): key: ExpectationKey; category: ResultCategory; active: bool; next_deadline: datetime | None; last_seen: datetime | None; status: Literal["ok", "due", "late", "silent", "suspended", "pending_pr"]; since: datetime | None
class CheckView(BaseModel): id: str; check_id: str; category: ResultCategory; cadence: Cadence; timezone: str; results_location: FileResults | MlflowResults; pr_number: int; agent: str; version: str | None; status: Literal["pending_pr", "active", "retired"]; registered: datetime   # chk_...

class Maintenance(Protocol):            # incidents task 7; until then contracts' default never suspends (open point 19); read by expectations and incidents
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
class SeverityPolicy(Protocol):         # incidents task 4; contracts' default returns medium
    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision: ...   # deterministic code, never a model: harm now, silence of a feed or job production depends on (second deadline: critical), cost of error, scope, confidence
class Delivery(BaseModel): person: str; channel: Literal["discord", "email"]; when: Literal["now", "morning_report"]; escalation_step: int
class RoutePlan(BaseModel): deliveries: list[Delivery]; inbox: bool; open_issue: bool
class Router(Protocol):                 # incidents task 4
    async def plan(self, project_id: str, incident: IncidentView, now: datetime) -> RoutePlan: ...   # critical: on-call at once at any hour, escalation after `escalation_timeout_minutes`; high: now inside the person's hours, else the morning report; medium, low: Inbox and the morning report; above the issue threshold: open_issue

WakeLabel = Literal["incident", "fix_request", "post_deploy", "follow_up"]
class AgentWaker(Protocol):             # incidents task 2 (early PR); no other code posts a system message to a session
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
class GateFacts(Protocol): async def facts(self, project_id: str, hook_id: str | None) -> tuple[bool | None, bool | None, bool | None]: ...   # (deploys verified, has production signal, team gate is authority); training task 7; contracts default: all None
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
class Baseline(Protocol):               # stats task 3; never notifies and never opens an incident
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
| `RegisterCheck(check_id, category, cadence, timezone, results_location, pr_number)` | read | results | S4 §4 |
| `IncidentUpdate(incident_id, cause?, category?, subtype?, status?, note?)` | read | incidents | S4 §5; status only `mitigated` or `resolved` |
| `IncidentMerge(ids)`, `IncidentSplit(id, signal_ids)` | read | incidents | S4 §5 |
| `IncidentFollowUp(incident_id, after_days?, after_labels?, note)` | read | incidents | not in the spec: S4 §3 and §6 need a way to register "re-check after N labels or days" (open point 3) |
| `AdjustSeverity(incident_id, direction, reason)` | read | incidents | Alert Prioritization only; not in the spec (open point 3) |
| `MonitoringQuery(what, ...)` | read | incidents | below |
| `RunHook(hook_id, reason, incident_id?, fix_id?, params?, rule_id?, policy_id?)` | deploy | hooks | `action_call` gives the hook's kind and the cited rule or policy |
| `Hold(incident_id, scope, reason)` | deploy | hooks | not in the spec (open point 3); gated as kind `hold` |
| `RequestFix(incident_id, evidence)` | read | training | S5 §1 |
| `RecordFixChoice(fix_id, choice, why, why_not)` | read | training | Retraining only; not in the spec |
| `SaveReport(draft)` | read | promotion | not in the spec: the report is stored before the `promotion` proposal cites it |
| `MergePullRequest(pr_number)` | deploy | promotion | S5 |

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
class ProtectionSurfaces(Protocol):     # promotion task 7
    async def paths(self, project_id: str) -> list[str]: ...                    # entry files of the model's evaluate and deploy hooks, every file under .github/workflows/, CODEOWNERS; open point 5
    async def touches(self, project_id: str, pr_number: int) -> list[str]: ...   # the surface paths the PR changes; the agent's labels are never read
class MergeCheck(BaseModel): ok: bool; refusals: list[str]       # own PR, mergeable and clean, no admin override, at least one passing check, no protection surface
```

- `MergePullRequest` is in code: an agent merges only its own PR; the platform reports it mergeable and clean; at least one check passed (a repository without CI never self-merges, nor does a local review); never an admin override; never a PR that touches a protection surface, at any level. Merges write `pr.merged_by_agent`.

### I39: the Dashboard's data (`GET /api/projects/{id}/dashboard`, ui task 1)

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

- Environments come from tracker aliases and the handoff's `ModelSpec.environments`; decisions from `Promotions.history`, `hook_runs` of kind rollback and fall_back (with their rule), and tracking's deployments.

### I41: Stats (`stats/contract.py`, stats task 4)

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

### I42: Usage (`providers/usage.py`, ui task 6)

```python
class ModelPrice(BaseModel): prompt_per_mtok: float; completion_per_mtok: float; source: Literal["provider", "user"]   # USD per million tokens
class UsageRow(BaseModel): project_id: str; agent: str; model_address: str; week_start: date; prompt_tokens: int; completion_tokens: int; cost_usd: float | None   # None: no price for that model
class UsageView(BaseModel): rows: list[UsageRow]; unpriced_models: list[str]
```

- `GET /api/usage?since=` (app level) sums `llm_calls` of every project by agent, model and week.
- `ModelEntry` gains `price: ModelPrice | None` (columns in `app/0003_stage4_5.sql`, contracts task 2); `PUT /api/models/{address}/price` sets a user price. When a provider's model list publishes prices (OpenRouter's `/models`: `pricing.prompt` and `pricing.completion`), the price is stored with `source="provider"` when such a model is added or verified, and when `list_remote_models` reads the list; a user price is never overwritten.

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
| S4 §7 severity, routing, escalation, Alert Prioritization | incidents (severity, routing, AP); escalation: notifications task 4 |
| S4 §8 channels, links, on-call, report, failures | notifications; UI: ui task 3 |
| S4 §9 proposal kinds | contracts (payloads, names); each kind and card by its owner: results (`parser`), hooks (`hooks`, `rule`), incidents (`maintenance`), training (`training_policy`), promotion (`promotion`) |
| S4 §10 verdicts, labels, baseline | stats; buttons and the miss report: ui task 2 |
| S4 §10 Knowledge note per resolved incident | definitions (open point 8) |
| S4 §11 UI | ui |
| S4 §12 storage, roadmap changes | contracts; heartbeat exception: results |
| S4 §13 errors | results, hooks, notifications; scrubbing: all |
| S4 §14 testing, Done when | each plan its tests; the simulated night: definitions task 3 |
| S5 hard limits, `MergePullRequest` | promotion task 7 (merging); default branch only and pins: training; cells: contracts |
| S5 the principle, PR discipline | definitions (prompts); merge rules in code: promotion task 7 (open point 9) |
| S5 changes 1 to 6 | definitions (shadow, canary by PR), contracts (cells, override, keyed by model), training (`GateFacts`, policy authority), ui (a list of models) |
| S5 §1 fix requests, guards, breaker | training tasks 1, 2, 6 |
| S5 §2 hooks: placeholders, `ci_dispatch`, `deploys` verified; policy; linking back | training |
| S5 §3 evaluation record, comparability | promotion; the step in the team's pipeline: definitions |
| S5 §4 promotion modes, report | promotion |
| S5 §5 agents' instructions | definitions |
| S5 §6 UI (Deployments page) | data: promotion task 6; page: ui task 7 |
| S5 §7 storage | contracts |
| S5 §8 testing | each plan; end to end: definitions task 4 |
| Roadmap stage 6: Stats | stats (I41) |
| Roadmap stage 6: Activity, Knowledge, Usage | ui tasks 4, 5, 6 (I42) |

## Open points for the lead

Each has a recommended default; answer "defaults" to take them all, or name the ones to change. The plan that depends on a point does not start before it is answered.

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
- contracts: `storage/migrations/project/0007_stage4_5.sql`, `storage/migrations/app/0003_stage4_5.sql`, `storage/tables.py`, `ids.py`, `config.py` (`croniter`), `credentials/project_secrets.py` (`hook_ref`), `events/contract.py` (`EventType`), the frontend `PROJECT_EVENT_TYPES`, `agents/policy/table.py`, `agents/policy/actions.py`, `agents/approvals/gate.py`, `agents/approvals/authority.py`, `agents/runtime/hooks.py` (`GateCall.action`), `agents/tools/base.py` (`action_call`), `agents/tools/names.py`, `agents/pr_sync.py` (`PrObserver`, `sync_now`), `proposals/contract.py` and `proposals/handoffs.py` (`HandoffObserver`, `add_observer` only), `monitoring/settings.py`, `monitoring/jobs.py`, `monitoring/untrusted.py`, `monitoring/expr.py`, every `contract.py` of I22 to I42. A later plan that needs a change opens a Decision. The migration numbers assume stage 3's approvals-fixes takes project `0006`; if another stage 3 plan adds a project migration first, contracts renumbers to the next free one.
- results: `monitoring/results/`, `monitoring/scheduler/`, `monitoring/expectations/`; `api/security.py` and `api/hosts.py` (the heartbeat exception and the `public_url` host rule, only those lines); `tests/fixtures/results/`.
- incidents: `monitoring/incidents/` (severity and routing included), `monitoring/waker.py`, the maintenance kind.
- notifications: `notifications/`.
- hooks: `monitoring/hooks/` except the files training owns; `monitoring/rules/`; `sandbox/containers.py` (only the hook credential's injection into one container run).
- training: `training/`; `monitoring/hooks/placeholders.py`, `ci_dispatch.py`, `pins.py`, `alias_check.py`, `facts.py`; `integrations/github/dispatch.py`; in `tests/fakes/fake_github.py` and `fake_mlflow.py`, its own functions only.
- promotion: `promotion/`; `integrations/github/merge.py`, `agents/tools/merge.py`; in the fakes, its own functions only. Neither training nor promotion edits `integrations/github/api.py`.
- stats: `monitoring/verdicts/`, `stats/`, `frontend/src/pages/stats/`.
- ui: `frontend/src/pages/{dashboard,incidents,activity,knowledge,usage,deployments}/`, `frontend/src/pages/project-settings/ProjectSettingsPage.tsx` and `frontend/src/pages/integrations/*` (one line per section), `frontend/src/pages/inbox/InboxPage.tsx`, `frontend/src/pages/project-home/*` (the Dashboard replaces the project home), `providers/usage.py` and the price route (the providers files it touches are named after stage 3's rate-limits-fixes plan merges: that plan splits `providers/service.py`).
- definitions: `agents/definitions/*.md` (after stage 3 onboarding), `backend/tests/e2e/test_monitoring_night.py`, `backend/tests/e2e/test_fix_and_promotion.py`, `frontend/e2e/monitoring.spec.ts`, `frontend/e2e/actions.spec.ts`, their fixture project.
- Shared, each plan only its own lines: `agents/tools/__init__.py` (`default_registry`), `api/services.py` (every kind, job, runner, lifecycle, authority, observer and query provider is registered in `build_services` after the registries exist), `frontend/src/pages/agents/ApprovalCard.tsx` (one dispatcher line per proposal kind; the card in its own file). New pages carry their own `route.tsx` and nav entry.
- `frontend/openapi.json`: the plan that changes the API regenerates it.
