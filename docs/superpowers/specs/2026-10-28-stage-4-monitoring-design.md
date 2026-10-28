# Stage 4: The monitoring loop

Date: 2026-10-28. Status: approved by the lead on 2026-10-28; amended with the stage 5 spec (training policies, fix requests, evaluation in the team's pipeline, continuous PRs), approved with it on 2026-10-28.
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
- `file`: records found under a connection's path pattern (JSON Lines, JSON, CSV, log lines), read incrementally. Job runs a scheduler logs to files are a `file` source.
- `ci`: workflow runs from the tracking record (stage 3), mapped to records by a built-in mapping (a failed or cancelled run fires); no parser is proposed.
- `mlflow`: metric points from the tracking record, mapped by the threshold the handoff gives for that metric.

**Parsers.** A source's raw output is turned into standard records by a parser. Agents' checks write standard records directly and need no parser. For the team's existing monitoring, Onboarding (or Data Quality) writes a parser per source, tests it on real outputs, and proposes it (proposal kind `parser`, §9). Approved parsers are stored versioned in `project.db`.
- **A mapping first.** A parser is declarative: field paths or column names for each standard field, an optional pattern for log lines, and a "fired when" expression over the mapped fields. The app evaluates it itself, with no code and no Docker, so reading never depends on the sandbox.
- **Code only when a mapping cannot express it.** Then the parser is a small script run in the sandbox over the read-only mount, with no network and a time limit.
- Either way, the output is validated against the standard record before it is stored. A parser that fails or produces invalid records raises an `operational` signal about itself; a code parser that cannot run (Docker down) does too.

**Catching up never pages anyone for the past.** After downtime, a signal whose condition has already cleared becomes an incident marked "resolved while the app was offline": it wakes no one and goes into the next report. Only conditions still true notify. Detection time counts from `seen_at`, honestly.

**Reading never misses or repeats.** Each source keeps a cursor: per file, its size, modified time and the byte offset read (an appended JSON Lines file is read from its offset); for `ci` and `mlflow`, the last run ID or time. A file that shrank or changed before the offset (restated data) is read again from the start. Records are idempotent by source and source key (from the record, else the file, offset and line), so a re-read stores nothing twice. After downtime the scheduler reads every source from its cursor, oldest first, before the live loop resumes.

**Times.** `ran_at` (the time the result describes) decides silence, ordering and `started_at`; `seen_at` decides detection time. Cadences, deadlines, on-call hours and the morning report use the IANA time zone given with them in the handoff, else the project's time zone (a new project setting, defaulting to the server's), daylight saving included.

**Untrusted text.** Everything read from the team's data, results, logs and parsers is data, never instructions. It reaches agents marked as untrusted content, the agents' prompts say so, and any action it might suggest still goes through the gates and the rules evaluated in code.

## 3. The app's scheduler

One loop in the app process with its own job table in `project.db` per project (it does not use stage 2's agent wake-ups, whose per-agent caps guard against an agent looping and would cap a bad night):
- sync tracking (stage 3);
- sync PR states (stage 2's PR state sync, run on a timer as well as after turns), so a check PR merged at night activates its expectation;
- collect results from every source (every minute by default);
- check expected results against their deadlines (§3a);
- incident follow-ups: "re-check after N days" by time, "re-check after N labels" by counting the labels table the handoff names, at each collection;
- escalate unacknowledged critical incidents (§7);
- send the morning report (§8).

Agents' own wake-ups stay stage 2's, unchanged. Jobs are idempotent and keyed by their due time; after downtime the loop runs every missed job in the order above (tracking first, so `ci` and `mlflow` sources are current before silence is judged), so the night's incidents are raised and diagnosed in order. The scheduler never runs a check or a model.

**3b. A heartbeat outside the app.** Only the app notifies, so an app that is down pages nobody. The app serves a heartbeat route (`GET /api/health/heartbeat`, no session needed, no data). In server mode, Onboarding's handoff proposes, and the Deployment & Rollback agent delivers by PR, a scheduled job in the team's own scheduler that calls it and alerts through the team's own channel when it fails twice. In local mode the heartbeat is not reachable from outside, and the Dashboard says so.

**3a. Expected results and silence.** From the handoff's operations (every feed and job with its expected time or cadence and its grace period) the app keeps expectations. When the deadline plus grace passes with no result or no new data, it raises a `silence` signal (category `operational`, or `data_quality` for a data feed). Maintenance windows (§5) suspend expectations.

## 4. Checks written by agents

- Agents (Data Quality, Model Performance, Retraining, Deployment & Rollback) write checks as code in the team's repository, test them in the sandbox on the mounted data, and deliver them by PR, adding each to the team's scheduler (a CI workflow, a cron entry, an Airflow task, whatever the handoff recorded).
- A check writes standard result records (§2) to the results location named in the handoff (a folder a connection reads, or the tracker's metrics), with its `check_id`, `origin: "agent"` and the version (git commit) that produced it.
- The agent registers each check it delivers with `RegisterCheck(check_id, category, cadence, timezone, results_location, pr_number)` (risk class `read`: it writes only the app's own records). The expectation becomes active when the PR merges (stage 2's PR state sync), so a merged check that never runs raises a silence signal instead of staying invisible. A check's version is the git commit in its records; a changed threshold is a new version, and verdicts keep the version they judged.
- Data Quality keeps two families (validation and drift) and classifies each change as bad data or real drift before acting, and explains every flag. Model Performance uses metrics suited to the problem and the error costs (PR-AUC, recall at a set precision, per-group metrics, calibration; never accuracy alone), accounts for label delay, and reads labels and predictions from the tables the handoff gave the `labels` and `predictions` roles, joined on the handoff's ID column. Its first audit also compares serving inputs with the training data (training-serving skew). Every check has a minimum sample size and records "not enough data" instead of firing on too little.

## 5. Incidents

**Signals** (`sig`): a fired result, a silence, a failed job or run, a parser failure. Each keeps its source record.

**Grouping.** A new signal joins an open incident when it shares a cause key (the same table, feed, job or model version) within a window (a setting), **or when its subject is downstream of the incident's subject in the pipeline graph** from the handoff (one upstream schema change that breaks scoring, shifts three columns and drops a metric is one incident). Otherwise it opens a new incident. The diagnosing agent can merge or split incidents (tools below).

**The incident** (`inc`): category (data quality, drift, model performance, operational), subtype (schema change, late feed, null spike, input drift, label shift, concept drift, failed training, bad deploy, failed scoring, silence, ...), severity (§7), status, cause (the agent's diagnosis), the signals, the related PRs and issues, `started_at` (the earliest signal's `ran_at`), `detected_at`, `acknowledged_at`, `mitigated_at`, `resolved_at`.

**The timeline** is not stored twice: it is the project's events (stage 2's event log) whose subject is the incident, so every action, notification, decision and verdict is recorded once.

**Lifecycle:** `open` → `acknowledged` (by a person) → `mitigated` (harm stopped: held, rolled back) → `resolved` (cause fixed or the condition gone, confirmed by results) → `closed` after the verdict. A resolved incident whose cause key fires again within a reopen window (a setting) is `reopened` instead of duplicated.

**Quiet period.** While an incident is open, new signals with its cause key attach to it and do not notify again; a severity increase does.

**Maintenance windows and snoozing.** The team declares windows (time range, the feeds, jobs or connections affected, a reason); expectations and signals in their scope are recorded but raise nothing. A person can snooze one incident until a time.

**Tools** (justified: incidents live in the app's database): `IncidentUpdate(incident_id, cause?, category?, subtype?, status?, note?)`, `IncidentMerge(ids)`, `IncidentSplit(id, signal_ids)`; risk class `read` (they change only the app's own records). An agent may set `mitigated` and `resolved`, the latter only when the latest results for the incident's cause key no longer fire (checked in code). Only a person acknowledges, snoozes or closes an incident.

## 6. Diagnosis and response

- When an incident opens or its severity rises, the app wakes the matching agent in its session, labeled with the incident: data quality and drift → Data Quality; model performance → Model Performance; failed training → Retraining; bad deploy, failed scoring → Deployment & Rollback; other operational → the Orchestrator.
- The agent investigates (hooks, Bash on the mounted data, the tracking record, notes, past incidents), records the cause, and chooses from the response menu, safest first:
  1. observe with a deadline (a follow-up wake-up: "re-check after N labels or N days");
  2. notify, or open an issue (stage 2's issue tools, above the team's priority threshold);
  3. hold or quarantine;
  4. roll back or fall back;
  5. rerun a job that is safe to rerun;
  6. request a fix from Retraining (stage 5: re-threshold, recalibrate or retrain, then evaluation and a promotion through the team's own mechanism);
  7. revert an agent PR that caused the incident (stage 5).
- **Action hooks.** Responses 3 to 5, and stage 5's training and promotion, run only the team's commands from the handoff, registered as action hooks (`hok`), each with its kind (rollback, rerun, hold, fall back, train, evaluate, deploy) and command. An `evaluate` hook is gated like `train`. Running a hook is risk class `deploy`.
- **Hook credentials.** A hook that writes to the team's systems (a rollback moving the tracker's production alias) has its own credential, entered by the user through a keystore route like a tracker credential. It is injected only into that hook's own container run, never into the agent's Bash, so the sandbox still never writes to the tracker on its own (stage 3).
- **Hold** is tier 1: the app records the hold on the incident and, when the team registered a hold or quarantine command, runs it (blocking a promotion, quarantining a batch) without asking. When the team has no such command, a hold is a notification only; the first audit reports the missing command as a gap.
- **Pre-approved rules** (`rul`), proposed by agents and approved by the user (proposal kind `rule`, §9): a condition over standard result records (check IDs, values, thresholds, a time window), the hook it allows (only kinds rollback, rerun, fall back), and limits (at most once per incident, per day). When an agent runs a hook citing a rule, **the gate evaluates the rule's condition in code against the stored records**; only when it holds, and the limits allow, does the hook run without asking (at any level except `suggest`). The agent can cite a rule; it can never certify one. The incident's timeline records the rule and the records that satisfied it. Otherwise the hook asks as `deploy` does.
- **A human decides** a deploy hook (tier 3) at every level below `bypass`; at `bypass` the person delegated it (stage 5). At night it waits for the person; the morning report lists it. A train hook runs without asking only within an approved training policy (stage 5); otherwise it asks.
- After a deploy (by the team's gate or a person), the deployment agent compares the new version with the previous one from the results the team's evaluation step and checks write (stage 5), and early labels with their counts; clear harm past a rule's threshold → roll back; unclear → observe with a deadline.
- **Fixing causes.** Beyond responding, agents fix what caused an incident through PRs to the team's system (a missing validation, a fragile job, a weak check), continuously, as stage 5's principle says.
- **Recent changes are suspects.** Incidents that open within a window (a setting) after any change merged or deployed (the agents' or the team's) are linked to those changes on their timeline. Agents may revert their own change (a revert PR marked as fixing the incident); for a team change, they comment on it and notify.

## 7. Severity and routing (Alert Prioritization)

- **Paging never waits for a model.** Severity and routing are deterministic code, run the moment an incident opens or changes. A critical incident is sent at once, even if every provider key is exhausted or the diagnosing agent is still working; the message says the diagnosis follows.
- Alert Prioritization then runs in the background (`chat: false`), once per new or changed incident: it writes the ranking's explanation, merges duplicates it finds, and may raise or lower the severity by one level with a stated reason, which re-routes.
- **Severity** (`critical`, `high`, `medium`, `low`) from, in order:
  - production harmed now (failed scoring, a model producing wrong outputs, a deploy past a rule's harm threshold) → critical;
  - silence of a feed or job production depends on → high, critical if past a second deadline;
  - the cost of error from the handoff (a miss costlier than a false alarm raises model performance incidents);
  - scope (all predictions or one group);
  - confidence (sample size; "not enough data" lowers it).
  The rules are deterministic code.
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
- Labels confirm or refute model performance incidents automatically once enough arrive (the minimum sample size): confirmation reads the standard results of Model Performance's label-based checks (agent-written, run on the team's scheduler, part of its first audit's expected output); it is recorded as a verdict with source `labels`.
- **Baseline record.** The team's existing monitoring (`origin: "team"`) is read and stored like every result, from the first one, and never notifies anyone or opens incidents on its own. For each incident, the app records whether and when a team check fired on the same cause key. Existing-monitoring alerts the agents stayed silent on are listed for verdicts. This is what stage 6's Stats and the evaluation compare.

## 11. UI

- **Dashboard** (one screen): the pipeline map from the handoff (stages, with their latest status), each production model and its latest metrics, open incidents by severity, expectations at risk, and what each agent is doing.
- **Inbox:** incidents ranked by severity, with acknowledge, snooze, verdict; approvals as today.
- **Incident page:** timeline, signals, cause, actions, related PRs and issues, verdict.
- **Integrations:** Discord and email with Test.
- **Project settings:** on-call people, maintenance windows, rules (enable or disable), action hooks, the collection interval, the escalation timeout, the issue threshold.

## 12. Storage

New in `project.db`: results, sources and cursors, parsers (versioned), expectations, scheduler jobs, registered checks, signals, incidents (their timeline is events), verdicts, rules, action hooks, maintenance windows, on-call people, notification log. App settings: Discord and email. One plan (the stage 4 contracts plan) writes the schema.

## Changes to the roadmap

- Run modes: one exception to "every API request needs a valid session": `GET /api/health/heartbeat`, which carries no data, takes no input and changes nothing. It is answered without a session and without the Origin check; the Host check accepts the configured `public_url` host.
- The permission gate's inputs grow: besides the risk class and the level, a call to an action hook passes its hook kind and the rule it cites, and the gate evaluates that rule in code before deciding (§6). A change to the merged gate and policy contract.
- ID prefixes `res` (result), `sig` (signal), `inc` (exists), `rul` (rule), `hok` (action hook), `win` (maintenance window), `chk` (registered check).
- Stage 4 "Agreed" rewritten from this spec: checks in the team's repository, delivered by PR; results read through sources and parsers; silence as a signal; the three tiers and the response menu; severity and routing; the baseline as the team's existing monitoring, read not re-run.
- The risk-class table's `propose` row lists parsers, rules, action hooks and maintenance windows; checks are delivered by PR (risk class `github`).
- Risk classes: the `deploy` exceptions are one table in the roadmap (Permission risk classes), updated with stage 5; this spec does not restate it.
- Stage 5's "train/deploy hooks" become the action hooks of this stage. Stage 5 keeps Retraining's and Deployment & Rollback's behavior (when to propose a retrain, evaluating a candidate against production, promotion) and the Deployments page.

## 13. Error handling

- A source that cannot be read raises an `operational` signal about the source, keeps its cursor, and retries on the next collection.
- A parser that fails is disabled after N consecutive failures, with an incident; its source is not silently skipped.
- A hook that fails is recorded on the incident; the agent chooses again; nothing retries a hook blindly.
- Every message, log and stored error goes through the existing secret scrubbing.

## 14. Testing

- Fakes: result sources from fixture folders, the fake MLflow and GitHub servers (stage 3), a fake SMTP server and a fake Discord webhook, a fake clock for the scheduler.
- Results: parsing to standard records; idempotent reading; an appended file is read from its offset and a restated one from the start; a cursor survives a restart; catching up after downtime reads every missed record oldest first, raises the night's incidents in order, and counts nothing twice.
- Silence: a missing result past deadline and grace raises one signal; a maintenance window suspends it.
- Incidents: grouping by cause key and down the pipeline graph; quiet period; reopen within the window; merge and split; lifecycle timestamps; an agent cannot close, and can resolve only when the results stopped firing.
- Checks: a registered check whose PR merged and that never writes a record raises a silence signal.
- Times: a cadence across a daylight-saving change fires once, at the right local time.
- Untrusted text: instructions planted in a result's detail or a data value never trigger an action.
- Severity and routing: each rule; critical notifies at night with every provider key exhausted and before any diagnosis, and escalates when unacknowledged; high waits for the person's hours; the morning report lists the night.
- Response: a hook runs only through an approved rule, a training policy or an approval; deploy always asks; a rule's and a policy's limits hold.
- Verdicts: labels confirm automatically past the minimum sample; the baseline record links team checks to incidents and never notifies.
- End to end (fake model, fixture project): a fired team check and an agent's check, a silent feed at night, one incident per cause, the right agent diagnoses, a rollback through an approved rule, a critical message to the fake Discord, the morning report, a verdict.

## Done when

- The end-to-end test passes and CI is green on every job.
- After a simulated night with the app stopped halfway, restarting it catches up with no missed and no duplicated incident.
- No deploy happens outside the team's own promotion mechanism: a person's approval, or, where the handoff makes the team's gate the authority, that gate within an approved training policy; no hook runs outside an approval, an approved rule or an approved training policy.

## Build order

1. Contracts: the records, IDs, schema, proposal kinds, the policy exceptions.
2. In parallel: results and parsers; the scheduler and expectations; incidents and their lifecycle; notifications and channels; rules and action hooks.
3. Alert Prioritization and the agents' definitions; verdicts and the baseline record.
4. UI: Dashboard, incident page, Inbox incidents, settings and Integrations.
