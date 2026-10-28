# Stage 4: The monitoring loop

Date: 2026-10-28. Status: draft, not approved.
Contracts (IDs, folder layout, SQLite rules, agent definitions, risk classes, event and audit records) are in `docs/roadmap.md`; this spec changes them there first where it says so. Background and decisions: `project_knowledge.md` (entries of 2026-10-27 and 2026-10-28). Stages 1 to 3 stay as their specs say, unless changed here.

## Goal

The app watches a production ML system at any hour and is the first to know when data, training, promotion, deployment, scoring or the monitoring itself goes wrong. It turns raw signals into a few incidents, has the right agent diagnose each one, takes the least risky step the team allows, tells the right person at the right time (waking the on-call person only when production is harmed), and learns from every incident. It does this for any production ML system, through the team's own infrastructure: the team's checks run on the team's scheduler; the app reads their results.

Principles (`project_knowledge.md`):
- The app is an extension of the team's system, never its production runtime. Checks are code in the team's repository, delivered by PR and run by the team's scheduler; the app reads every result and never re-runs the team's checks.
- Who decides, in three tiers: always allowed (watch, diagnose, record, notify, hold a pending deploy); pre-approved rules (act within limits the team set in advance); a human always decides (deploying a new model, anything irreversible).
- The least risky move comes first.
- The app runs continuously; after downtime it catches up.

## Scope

In:
- Result sources and parsers: every result the app can read becomes one standard result record.
- Expected results and silence: every feed and job from the handoff has a deadline; a miss is a signal.
- The app's scheduler: collecting results, deadlines, wake-ups, notifications, the morning report, catching up after downtime. It never runs a check.
- Checks written by agents: delivered by PR, writing standard result records.
- Incidents: grouping signals by cause, the lifecycle, severity, escalation, quiet periods, maintenance windows, snoozing.
- Diagnosis by the matching agent and the response menu, within the three tiers; pre-approved rules; action hooks (the team's commands).
- Alert Prioritization: ranking and routing.
- Notifications: Discord and email; on-call; the morning report.
- Verdicts, automatic confirmation by labels, and the baseline record (the team's existing monitoring).
- The Dashboard, the Inbox's incidents, the Integrations settings for Discord and email.

Out:
- Reading metrics systems (Prometheus, Datadog and the like): real-time serving numbers are read only where the team already writes them somewhere the app reads (files, CI, the tracker).
- Acknowledging alerts by replying in Discord or email: the user acknowledges in the app.
- Slack, WhatsApp, paging services (PagerDuty, Opsgenie): later notification channels.
- The Stats page and its metrics (stage 6), retraining and deployment behavior beyond running the team's commands (stage 5). Stage 4 records everything they read.

## Answers to the roadmap's stage 4 questions

- The incident data model and lifecycle: §5.
- How Alert Prioritization ranks: §7.
- The scheduler inside the single process: §3.
- How checks written by agents are stored, versioned and run: in the team's repository, delivered by PR, run by the team's scheduler, versioned by git (§4). The app stores only the parsers and the expectations it reads them with.

## 1. Module layout

```
backend/src/mlops_agents/
  monitoring/
    results/        result sources, parsers, the standard result record, reading and catching up
    expectations/   expected results and silence
    scheduler/      the app's scheduler (collection, deadlines, wake-ups, notifications, reports)
    incidents/      signals, grouping, incidents, lifecycle, severity, escalation, windows
    rules/          pre-approved rules and their evaluation
    hooks/          action hooks: the team's commands as deploy-class actions
  notifications/    Discord and email senders, on-call routing, the morning report
  agents/definitions/  Data Quality, Model Performance, Alert Prioritization, Deployment & Rollback, Retraining updated
```

## 2. Results

**The standard result record** (`res`), stored in `project.db`:
`check_id`, `origin` (`team` for the handoff's existing monitoring, `agent` for checks agents delivered), `ran_at` (the time the result describes, from the source), `seen_at` (when the app read it), `fired` (bool), `value` (number or null), `threshold` (number or null), `category` (data quality, drift, model performance, operational), `subject` (what it is about: a table, column, model version, job), `detail` (JSON), `source_ref` (where it came from).

**Sources**, one interface per kind, read every minute (a project setting) by the scheduler:
- `file`: records found under a connection's path pattern (JSON Lines, JSON, CSV), read incrementally by data version.
- `ci`: workflow runs from the tracking record (stage 3).
- `mlflow`: metric points from the tracking record.
- `job`: job runs the team records in a file (as many schedulers write logs) or CI.

**Parsers.** A source's raw output is turned into standard records by a parser. Agents' checks write standard records directly and need no parser. For the team's existing monitoring, Onboarding (or Data Quality) writes a parser per source, tests it on real outputs, and proposes it (proposal kind `parser`, §9). Approved parsers are stored versioned in `project.db`.
- **A mapping first.** A parser is declarative: field paths or column names for each standard field, an optional pattern for log lines, and a "fired when" expression over the mapped fields. The app evaluates it itself, with no code and no Docker, so reading never depends on the sandbox.
- **Code only when a mapping cannot express it.** Then the parser is a small script run in the sandbox over the read-only mount, with no network and a time limit.
- Either way, the output is validated against the standard record before it is stored. A parser that fails or produces invalid records raises an `operational` signal about itself; a code parser that cannot run (Docker down) does too.

**Catching up never pages anyone for the past.** After downtime, a signal whose condition has already cleared becomes an incident marked "resolved while the app was offline": it wakes no one and goes into the next report. Only conditions still true notify. Detection time counts from `seen_at`, honestly.

**Reading never misses or repeats.** Each source keeps a cursor (its last data version, run ID or time). Records are idempotent by source and source key. After downtime the scheduler reads every source from its cursor, oldest first, before the live loop resumes; nothing is dropped and nothing is counted twice.

## 3. The app's scheduler

One loop in the app process, built on stage 2's wake-up timer, with one job table in `project.db` per project:
- collect results from every source (every minute by default);
- check expected results against their deadlines (§3a);
- fire agents' wake-ups (stage 2) and incident follow-ups ("re-check after N days or N labels");
- escalate unacknowledged critical incidents (§6);
- send the morning report (§8);
- sync tracking (stage 3).

Jobs are idempotent and keyed by their due time; after downtime the loop runs every missed job in order (collection first, then deadlines, then follow-ups), so the incidents of the night are raised and diagnosed in the right order. The scheduler never runs a check or a model.

**3a. Expected results and silence.** From the handoff's operations (every feed and job with its expected time or cadence and its grace period) the app keeps expectations. When the deadline plus grace passes with no result or no new data, it raises a `silence` signal (category `operational`, or `data_quality` for a data feed). Maintenance windows (§5) suspend expectations.

## 4. Checks written by agents

- Agents (Data Quality, Model Performance, Retraining, Deployment & Rollback) write checks as code in the team's repository, test them in the sandbox on the mounted data, and deliver them by PR, adding each to the team's scheduler (a CI workflow, a cron entry, an Airflow task, whatever the handoff recorded).
- A check writes standard result records (§2) to the results location named in the handoff (a folder a connection reads, or the tracker's metrics), with its `check_id`, `origin: "agent"` and the version (git commit) that produced it.
- When the PR merges and the first record arrives, the app adds the check to the expectations with its cadence.
- Data Quality keeps two families (validation and drift) and classifies each change as bad data or real drift before acting, and explains every flag. Model Performance uses metrics suited to the problem and the error costs (PR-AUC, recall at a set precision, per-group metrics, calibration; never accuracy alone), and accounts for label delay. Every check has a minimum sample size and records "not enough data" instead of firing on too little.

## 5. Incidents

**Signals** (`sig`): a fired result, a silence, a failed job or run, a parser failure. Each keeps its source record.

**Grouping.** A new signal joins an open incident when it shares a cause key (the same table, feed, job or model version) within a window (a setting), **or when its subject is downstream of the incident's subject in the pipeline graph** from the handoff (one upstream schema change that breaks scoring, shifts three columns and drops a metric is one incident). Otherwise it opens a new incident. The diagnosing agent can merge or split incidents (tools below).

**The incident** (`inc`): category (data quality, drift, model performance, operational), subtype (schema change, late feed, null spike, input drift, label shift, concept drift, failed training, bad deploy, failed scoring, silence, ...), severity (§7), status, cause (the agent's diagnosis), the signals, the timeline (every agent action, notification, decision and verdict), the related PRs and issues, `started_at` (the earliest signal's `ran_at`), `detected_at`, `acknowledged_at`, `mitigated_at`, `resolved_at`.

**Lifecycle:** `open` → `acknowledged` (by a person) → `mitigated` (harm stopped: held, rolled back) → `resolved` (cause fixed or the condition gone, confirmed by results) → `closed` after the verdict. A resolved incident whose cause key fires again within a reopen window (a setting) is `reopened` instead of duplicated.

**Quiet period.** While an incident is open, new signals with its cause key attach to it and do not notify again; a severity increase does.

**Maintenance windows and snoozing.** The team declares windows (time range, the feeds, jobs or connections affected, a reason); expectations and signals in their scope are recorded but raise nothing. A person can snooze one incident until a time.

**Tools** (justified: incidents live in the app's database): `IncidentUpdate(incident_id, cause?, category?, subtype?, status?, note?)`, `IncidentMerge(ids)`, `IncidentSplit(id, signal_ids)`; risk class `read` (they change only the app's own records).

## 6. Diagnosis and response

- When an incident opens or its severity rises, the app wakes the matching agent in its session, labeled with the incident: data quality and drift → Data Quality; model performance → Model Performance; failed training → Retraining; bad deploy, failed scoring → Deployment & Rollback; other operational → the Orchestrator.
- The agent investigates (hooks, Bash on the mounted data, the tracking record, notes, past incidents), records the cause, and chooses from the response menu, safest first:
  1. observe with a deadline (a follow-up wake-up: "re-check after N labels or N days");
  2. notify, or open an issue (stage 2's issue tools, above the team's priority threshold);
  3. hold or quarantine;
  4. roll back or fall back;
  5. rerun a job that is safe to rerun;
  6. retrain or deploy something new.
- **Action hooks.** Responses 3 to 6 run only the team's commands from the handoff, registered as action hooks (`hok`), each with its kind (rollback, rerun, hold, fall back, train, deploy) and command. Running a hook is risk class `deploy`.
- **Hook credentials.** A hook that writes to the team's systems (a rollback moving the tracker's production alias) has its own credential, entered by the user through a keystore route like a tracker credential. It is injected only into that hook's own container run, never into the agent's Bash, so the sandbox still never writes to the tracker on its own (stage 3).
- **Hold** is tier 1: the app records the hold on the incident and, when the team registered a hold or quarantine command, runs it (blocking a promotion, quarantining a batch) without asking. When the team has no such command, a hold is a notification only; the first audit reports the missing command as a gap.
- **Pre-approved rules** (`rul`), proposed by agents and approved by the user (proposal kind `rule`, §9): a condition over standard result records (check IDs, values, thresholds, a time window), the hook it allows (only kinds rollback, rerun, fall back), and limits (at most once per incident, per day). When an agent runs a hook citing a rule, **the gate evaluates the rule's condition in code against the stored records**; only when it holds, and the limits allow, does the hook run without asking (at any level except `suggest`). The agent can cite a rule; it can never certify one. The incident's timeline records the rule and the records that satisfied it. Otherwise the hook asks as `deploy` does.
- **A human always decides** a train or deploy hook (tier 3): it always asks, `bypass` included, like `propose`. At night it waits for the person; the morning report lists it.
- After a deploy, the deployment agent compares the new version with the previous one on recent production inputs (both loaded from the tracker, scored in the sandbox) and on early labels with their counts; clear harm past a rule's threshold → roll back; unclear → observe with a deadline.

## 7. Severity and routing (Alert Prioritization)

- Alert Prioritization runs in the background (`chat: false`), once per new or changed incident, after the diagnosis or after a short wait if none comes.
- **Severity** (`critical`, `high`, `medium`, `low`) from, in order:
  - production harmed now (failed scoring, a model producing wrong outputs, a deploy past a rule's harm threshold) → critical;
  - silence of a feed or job production depends on → high, critical if past a second deadline;
  - the cost of error from the handoff (a miss costlier than a false alarm raises model performance incidents);
  - scope (all predictions or one group);
  - confidence (sample size; "not enough data" lowers it).
  The rules are deterministic code; the agent may raise or lower by one level with a stated reason, recorded on the timeline.
- **Routing:**
  - critical: Discord and email to the on-call person at once, at any hour; if not acknowledged within N minutes (a setting), to the next person in the escalation list;
  - high: during the person's hours at once, otherwise in the morning report;
  - medium and low: the Inbox and the morning report;
  - above the team's issue threshold (stage 2 setting): a GitHub issue.

## 8. Notifications and the morning report

- **Channels:** Discord (a webhook URL) and email (SMTP: host, port, user, sender; the password in the keystore), set in Integrations with a Test button. A message carries the incident's title, severity, cause, what was done, what waits for a person, and a link to it in the app.
- **Links.** Links use the app's `public_url` setting (server mode, behind HTTPS). In local mode the app is reachable only from its own machine, so a phone cannot open the link; the message says so and carries the full summary.
- **"Waking" someone** is a Discord mention and an email marked urgent; whether it rings depends on the person's phone settings, which the Integrations page tells them.
- **On-call:** people from the handoff (name, Discord mention or email, hours, time zone, escalation order), editable in project settings.
- **The morning report**, at a set time per person: incidents since the last report, what agents did and why (each with the rule or approval behind it), what waits for a person, and expectations that were suspended by maintenance.
- A notification that fails to send is retried and recorded; a channel failing repeatedly raises an `operational` incident in the app.

## 9. Proposal kinds (stage 3's mechanism)

- `parser`: a source, its parser code, sample input and the records it produced in the sandbox. Applying stores it versioned.
- `rule`: a pre-approved rule (§6). Applying stores it; rules can be disabled in settings.
- `hooks`: the team's action commands to register (from the handoff, or found later). Applying registers them.
- `maintenance`: a window agents suggest (for example from an upstream announcement). Applying stores it.
All ask at every level, `bypass` included, as stage 3 decided.

## 10. Verdicts and the baseline record

- Every incident has verdict buttons (real issue / not an issue / wrong type, with the right type), in the app and in the morning report's link. A user can also report a miss (something the app should have caught, with its time).
- Labels confirm or refute model performance incidents automatically once enough arrive (the minimum sample size); the confirmation is recorded as a verdict with source `labels`.
- **Baseline record.** The team's existing monitoring (`origin: "team"`) is read and stored like every result, from the first one, and never notifies anyone or opens incidents on its own. For each incident, the app records whether and when a team check fired on the same cause key. Existing-monitoring alerts the agents stayed silent on are listed for verdicts. This is what stage 6's Stats and the evaluation compare.

## 11. UI

- **Dashboard** (one screen): the pipeline map from the handoff (stages, with their latest status), the production model and its latest metrics, open incidents by severity, expectations at risk, and what each agent is doing.
- **Inbox:** incidents ranked by severity, with acknowledge, snooze, verdict; approvals as today.
- **Incident page:** timeline, signals, cause, actions, related PRs and issues, verdict.
- **Integrations:** Discord and email with Test.
- **Project settings:** on-call people, maintenance windows, rules (enable or disable), action hooks, the collection interval, the escalation timeout, the issue threshold.

## 12. Storage

New in `project.db`: results, sources and cursors, parsers (versioned), expectations, scheduler jobs, signals, incidents and their timeline, verdicts, rules, action hooks, maintenance windows, on-call people, notification log. App settings: Discord and email. One plan (the stage 4 contracts plan) writes the schema.

## Changes to the roadmap

- ID prefixes `res` (result), `sig` (signal), `inc` (exists), `rul` (rule), `hok` (action hook), `win` (maintenance window).
- Stage 4 "Agreed" rewritten from this spec: checks in the team's repository, delivered by PR; results read through sources and parsers; silence as a signal; the three tiers and the response menu; severity and routing; the baseline as the team's existing monitoring, read not re-run.
- Risk classes, `deploy` exceptions, as one table in the roadmap:

  | Hook kind | Matching approved rule, confirmed by the gate | No matching rule |
  |---|---|---|
  | train, deploy | asks at every level, `bypass` included | asks at every level, `bypass` included |
  | rollback, rerun, fall back | runs without asking at `ask`, `autonomous`, `bypass`; denied at `suggest` | the stage 2 `deploy` row |
  | hold | runs without asking at every level except `suggest` (tier 1) | same |
- Stage 5's "train/deploy hooks" become the action hooks of this stage. Stage 5 keeps Retraining's and Deployment & Rollback's behavior (when to propose a retrain, evaluating a candidate against production, promotion) and the Deployments page.

## 13. Error handling

- A source that cannot be read raises an `operational` signal about the source, keeps its cursor, and retries on the next collection.
- A parser that fails is disabled after N consecutive failures, with an incident; its source is not silently skipped.
- A hook that fails is recorded on the incident; the agent chooses again; nothing retries a hook blindly.
- Every message, log and stored error goes through the existing secret scrubbing.

## 14. Testing

- Fakes: result sources from fixture folders, the fake MLflow and GitHub servers (stage 3), a fake SMTP server and a fake Discord webhook, a fake clock for the scheduler.
- Results: parsing to standard records; idempotent reading; a cursor survives a restart; catching up after downtime reads every missed record oldest first, raises the night's incidents in order, and counts nothing twice.
- Silence: a missing result past deadline and grace raises one signal; a maintenance window suspends it.
- Incidents: grouping by cause key; quiet period; reopen within the window; merge and split; lifecycle timestamps.
- Severity and routing: each rule; critical notifies at night and escalates when unacknowledged; high waits for the person's hours; the morning report lists the night.
- Response: a hook runs only through an approved rule or an approval; train and deploy always ask; a rule's limits hold.
- Verdicts: labels confirm automatically past the minimum sample; the baseline record links team checks to incidents and never notifies.
- End to end (fake model, fixture project): a fired team check and an agent's check, a silent feed at night, one incident per cause, the right agent diagnoses, a rollback through an approved rule, a critical message to the fake Discord, the morning report, a verdict.

## Done when

- The end-to-end test passes and CI is green on every job.
- After a simulated night with the app stopped halfway, restarting it catches up with no missed and no duplicated incident.
- No train or deploy hook runs without a person's approval; no hook runs outside an approval or an approved rule.

## Build order

1. Contracts: the records, IDs, schema, proposal kinds, the policy exceptions.
2. In parallel: results and parsers; the scheduler and expectations; incidents and their lifecycle; notifications and channels; rules and action hooks.
3. Alert Prioritization and the agents' definitions; verdicts and the baseline record.
4. UI: Dashboard, incident page, Inbox incidents, settings and Integrations.
