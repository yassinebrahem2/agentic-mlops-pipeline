# Stage 4 — Notifications Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; tasks 1 and 2 get early PRs (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, and its tests (the task's stop condition). Read the [stages 4-6 overview](../2026-10-29-stage-4-6-overview.md) first (I24, I26 to I28, "Shared by contracts", "Routes", "Files shared"); this plan does not repeat it.

**Goal:** the app tells the right person at the right time (S4 §8): Discord and email channels with retries and a log, the Integrations settings behind them, the on-call people, escalation of unacknowledged critical incidents, and the morning report. Backend only: ui task 4 builds the Integrations and on-call screens on these routes. Who is told what and when is incidents' (the reactor sends every immediate delivery); this plan delivers, retries, escalates and reports.

**Branch:** `plan/stage-4-notifications`.

**Consumes** (none of the stage 4 interfaces is on `main` yet):
- contracts task 1: I28 in full (`DiscordSettings`, `EmailSettings`, `Message` with `lines`, `ChannelError`, `Channel`, `OnCallPerson`, `OnCallPeople`, `NotificationKind`, `Notifier`, `ReportAction`, `MorningReportBody` with `wakes_skipped`, `MorningReports`, `DISCORD_WEBHOOK_REF`, `EMAIL_PASSWORD_REF`, `PUBLIC_URL_KEY`, `DISCORD_SETTINGS_KEY`, `EMAIL_SETTINGS_KEY`); `Severity`, `IncidentView`, `SignalDraft`, `Incidents`, `ACTIVE_STATUSES` (I26); `ExpectationKey` (I25); `JobKind`, `JobDue`, `ScheduledJob` (I24); `HookRunView` (I29); `MonitoringSettings`; `EventLog.page`; the event types `notification.sent`, `notification.failed`, `escalation.sent`, `report.sent`, `incident.hold_recorded`, `incident.wake_skipped`; `HandoffObserver` and `Handoffs.add_observer`; the `notifications/` package.
- contracts task 2: the `notification_log` and `on_call_people` tables; the app `settings` rows need no DDL.
- results task 1 (early PR): `Scheduler.register`, `interval_dues`, `next_occurrences`, `Services.clock`. Tasks 1, 4, 5.
- incidents task 1 (early PR): `Incidents` (`list`, `get`, `raise_signal`). Tasks 4 and 5.
- results task 5 (early PR): `Expectations.states`. Task 5.
- hooks task 1 (early PR): `Hooks.runs`. Task 5.
- On `main`: `KeyStore`, `EventLog`, `Clock`, `credentials/scrub.py` (`scrub_values`, `error_text`, `base_url_problem`), `Approvals.pending`, `Settings.mode` (`RunMode`), `Handoffs.current`, `OnCall`/`Contact`/`OnCallHours`/`Cadence` (`proposals/payloads.py`), `project_settings`, `api/hosts.py` (`HostRules`, `HostCheck`), `include_routers`.

**Produces** (types are contracts'; this plan never redefines them):
- `IntegrationSettings`, the channels, `SqliteNotifier(Notifier)` and the `RETRY_NOTIFICATIONS` job (task 1, early PR). The fakes `backend/tests/fakes/fake_discord.py` and `fake_smtp.py` (task 1), which the definitions plan's end-to-end tests use.
- `SqliteOnCallPeople(OnCallPeople)` and `GET`/`PUT /api/projects/{id}/on-call` (task 2, early PR), for incidents task 4 and ui task 4.
- The Integrations routes, the heartbeat's `public_url` host rule, `incident_link` and `inbox_link` (task 3), for ui task 4.
- The `ESCALATE` job and the channel-failure signal (task 4); the `MORNING_REPORT` job with `SqliteMorningReports(MorningReports)` (task 5).

**Constants (one source, `notifications/constants.py`, created by task 1):** `SEND_TIMEOUT_S = 10`; `RETRY_BACKOFFS_S = (30, 120, 600)`; `FAILURE_SIGNAL_AFTER = 3` (consecutive deliveries on one channel that ended failed); `DISCORD_CONTENT_MAX = 2000`; `ERROR_MAX_CHARS = 500`; `INCIDENT_PATH = "/projects/{project_id}/incidents/{incident_id}"` and `INBOX_PATH = "/projects/{project_id}/inbox"` (task 3; ui's routes use the same paths); `ON_CALL_SEEDED_KEY = "on_call_handoff_version"` (task 2). The app setting keys and keystore refs are contracts' (`notifications/contract.py`); only `IntegrationSettings` reads and writes them.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | Channels, `Notifier`, the retry job, `notification_log`, the fakes | contracts tasks 1, 2; results task 1 | after contracts tasks 1, 2 and results task 1; **early PR** |
| 2 | `OnCallPeople`, its routes, seeding from the handoff | contracts tasks 1, 2 | after task 1; **early PR** |
| 3 | The Integrations routes: Discord, email, Test, `public_url`, links | task 1 | after task 2 |
| 4 | Escalation and the channel-failure signal | tasks 1, 2; incidents task 1 | after task 3 and incidents task 1 |
| 5 | The morning report | tasks 1, 2; incidents task 1; results task 5; hooks task 1 | after task 4, results task 5 and hooks task 1 |

Tasks 1 to 3 are the core path; 4 and 5 are extras (overview, Plans).

---

### Task 1: Channels, `Notifier`, the retry job, `notification_log`, the fakes

**Files:** new `backend/src/mlops_agents/notifications/constants.py`, `notifications/settings.py` (`IntegrationSettings`), `notifications/render.py`, `notifications/channels/__init__.py` (`default_channels(...)`), `notifications/channels/discord.py`, `notifications/channels/email.py`, `notifications/notifier.py` (`SqliteNotifier`), `notifications/retries.py` (`RetryJob`); `api/services.py` (its own lines: construction, `_app_secrets`, `scheduler.register`); new `backend/tests/fakes/fake_discord.py`, `backend/tests/fakes/fake_smtp.py` and their tests; tests under `backend/tests/notifications/`.

**Interfaces:**
- Consumes the I28 types, `Severity`, the `notification_log` table, the app `settings` table, `KeyStore`, `EventLog`, `Clock`, `ScheduledJob`, `interval_dues`, `MonitoringSettings.collection_interval_s`.
- Produces `IntegrationSettings(databases, keystore)`: `discord() -> DiscordSettings`, `email() -> EmailSettings`, `public_url() -> str | None`, their setters, `webhook_url() -> str | None`, `email_password() -> str | None`, `secret_values() -> list[str]` (the webhook URL, its token segment, the password). The one reader and writer of these settings.
- Produces `SqliteNotifier(databases, settings, channels, events, clock)` implementing `Notifier.notify`, and `RetryJob(ScheduledJob)` with `kind = JobKind.RETRY_NOTIFICATIONS`.

**Notes:**
- **App-level secrets do not go through `ProjectSecrets`** (it rejects refs outside `project/<id>/`). Read and write `DISCORD_WEBHOOK_REF` and `EMAIL_PASSWORD_REF` with `KeyStore` in `asyncio.to_thread`, as `integrations/github/auth.py` does for `GITHUB_KEYSTORE_REF`.
- **The webhook token is in the URL path.** `scrub_url_secrets` masks only userinfo and query values. Every `ChannelError` text, `notification_log` error, log line and event payload goes through `scrub_values(error_text(exc), settings.secret_values())`, capped at `ERROR_MAX_CHARS`. Add `secret_values()` to `_app_secrets` in `api/services.py` so the audit scrubber covers them. No event payload carries a URL, a password or a contact address: payloads carry only the person ID, the channel, the kind and the attempt.
- **One renderer** (`render.py`) turns a `Message` into the plain text both channels send: the title, severity, cause (or "diagnosis follows" when `diagnosis_pending`), done, waiting, `lines`, the link, and the local-mode note. A channel only wraps that text.
- **Discord:** POST `{"content", "allowed_mentions"}` to the webhook with the shared `httpx2` client and `SEND_TIMEOUT_S`. `allowed_mentions` is `{"parse": [], "users": [<the target's ID>]}` when `urgent`, else `{"parse": []}`, so agent-written text (a cause) can never ping `@everyone` or a role. The mention is prepended only when `urgent`. Content is cut at `DISCORD_CONTENT_MAX`. Any non-2xx (429 included) raises `ChannelError`.
- **Email:** stdlib `smtplib` in `asyncio.to_thread` with `SEND_TIMEOUT_S`; `email.message.EmailMessage`. CR and LF are stripped from the subject (header injection). `urgent` sets `Importance: high` and `X-Priority: 1`. Port 465 uses `SMTP_SSL`; otherwise STARTTLS when `starttls`. Log in only when a password is set, and never over a connection without TLS (a `ChannelError` naming the setting).
- **No new dependency** (Python 3.12 has no `smtpd`). `fake_smtp.py` is a small asyncio SMTP server on `127.0.0.1:0` (EHLO, AUTH PLAIN/LOGIN, MAIL, RCPT, DATA, QUIT, and a STARTTLS that answers 454); it records each message and can fail the next N sends. `fake_discord.py` follows `fake_github.py`: a FastAPI app with recorded posts, scripted failures (status codes, 429), `start_fake_discord`, and `python -m tests.fakes.fake_discord --port N`.
- **`notify`** delivers to every contact of the person whose channel is enabled and set up ("critical alerts go to all"). Each contact is one `notification_log` row, and the first attempt is inline. `escalation_step` on the row is 0 for kind `incident`, else the number of people already sent an `incident` or `escalation` row for that incident. A `ChannelError` leaves the row `retrying` with its next attempt at `RETRY_BACKOFFS_S[n]`; once the backoffs run out the row is `failed`. A contact whose channel is disabled or not set up is logged `failed` at once with that reason. The return value is `sent` if any contact sent, else `retrying` if any row is retrying, else `failed`. The row keeps the `Message` as JSON (no secret is in it), so a retry and escalation can send it again.
- **Retries:** `RetryJob.due` gives one due per collection interval (`interval_dues`); `run` attempts every `retrying` row whose next attempt is at or before now, for that project; a backoff shorter than the collection interval waits for the next run. Retries survive a restart (the rows are the record) and run on the app's one scheduler.
- **One event per delivery outcome, mapped in one place in the notifier:** a sent row records `notification.sent` (kinds `incident` and `promotion`), `escalation.sent` or `report.sent`; a row that ends `failed` records `notification.failed`. `subject_id` is the incident when there is one, so the incident's timeline holds each delivery once.
- The notifier fills `link` and `local_mode_note` itself from task 3 on; until then it sends the message as given.

**Tests (stop condition):** Discord and email each deliver a rendered message to the fakes. An urgent Discord message carries the mention and `allowed_mentions` naming only that user, and a cause containing `@everyone` pings no one. An urgent email has the priority headers. A subject with a newline is sent on one line. A password is never sent over a plain connection. A failure followed by success: the row goes `retrying` and then `sent` on the retry job's next run (fake clock). A failure after every backoff gives `failed` and `notification.failed`. A disabled channel is never attempted. A `retrying` row is resumed after a restart (a new notifier and job on the same data folder). A failing webhook's error, the stored row and the event contain neither the webhook URL nor its token. The audit scrubber masks the webhook URL and the email password. Sent events use the kind's type and the incident as `subject_id`. Full backend suite, ruff, format, pyright; CI green.

**PR:** its own PR into `main` from `plan/stage-4-notifications-task1`, as `AGENTS.md` rule 5 says (incidents task 4 consumes it).

---

### Task 2: `OnCallPeople`, its routes, seeding from the handoff

**Files:** new `backend/src/mlops_agents/notifications/on_call.py` (`SqliteOnCallPeople`, `OnCallSeeder`), new `api/routers/on_call.py`; `api/services.py` (its own lines: construction, `handoffs.add_observer(seeder)`, the startup reconcile); tests; regenerated `frontend/openapi.json`.

**Interfaces:**
- Consumes `OnCall`, `OnCallPerson`, `OnCallPeople`, `HandoffObserver`, `Handoffs.add_observer`, `Handoffs.current`, the `on_call_people` and `project_settings` tables.
- Produces `SqliteOnCallPeople(databases)` (`list`, ordered by `escalation_order` then `id`; `replace`); `OnCallSeeder(people, handoffs, databases)` implementing `HandoffObserver.stored`, plus `reconcile(project_id)`; and `GET`/`PUT /api/projects/{id}/on-call` (`list[OnCallPerson]` out, `list[OnCall]` in).

**Notes:**
- `id` is a slug of the name. A collision within one list gets a `-2`, `-3` suffix in list order, so `replace` never fails on names, and the same list always gives the same IDs. `replace` deletes and inserts in one transaction.
- **Do not wipe a person's edits.** `stored` replaces the list with the handoff's `operations.on_call` and records that handoff's version under `ON_CALL_SEEDED_KEY` in `project_settings`: a newly approved handoff replaces the list (a person approved it). `reconcile` (at startup, for every project) replaces only when `Handoffs.current()`'s version is newer than the recorded one, so a restart never overwrites edits made through the route.
- `stored` runs after the handoff's commit. A failure is logged scrubbed with the project ID and does not propagate into the handoff's apply; the next startup's reconcile repairs it. At startup, one project's failure is logged and the others still reconcile.

**Tests (stop condition):** `Handoffs.store` seeds the list through the observer, with slugs and order. A second handoff version replaces it. A route edit survives a restart's reconcile. A project onboarded before stage 4 (a handoff stored before the observer existed) is seeded by reconcile. Duplicate names get suffixed IDs. Invalid hours or a bad time zone gives 422. Full suite, ruff, format, pyright; CI green.

**PR:** its own PR into `main` from `plan/stage-4-notifications-task2`, as `AGENTS.md` rule 5 says (incidents task 4 consumes it).

---

### Task 3: The Integrations routes: Discord, email, Test, `public_url`, links

**Files:** new `backend/src/mlops_agents/api/routers/integrations.py` (prefix `/integrations`; `github.py` is not touched), new `notifications/links.py`; `notifications/notifier.py` (link filling); `api/hosts.py` (the heartbeat's `public_url` host only); `notifications/settings.py` (the cached `public_url` host); tests under `backend/tests/notifications/` and `backend/tests/api/`; regenerated `frontend/openapi.json`.

**Interfaces:**
- Consumes task 1 (`IntegrationSettings`, the channels, `render`), `Settings.mode`, `HostCheck`.
- Produces the routes of the overview's "Routes" row for notifications task 3:
  - `GET`/`PUT /api/integrations/discord`: `DiscordSettings`; the body is `{enabled, webhook_url?}`, with `webhook_url` as `Field(repr=False)`. `DELETE /api/integrations/discord/webhook`.
  - `GET`/`PUT /api/integrations/email`: `EmailSettings`; the body is the settings plus `password?` (`repr=False`). `DELETE /api/integrations/email/password`.
  - `POST /api/integrations/{discord|email}/test`, body `{target}`: returns `ChannelTest(ok: bool, detail: str | None)` (a router-local model).
  - `GET`/`PUT /api/integrations/public-url`: `{public_url: str | None}`.
- Produces `incident_link(public_url, project_id, incident_id)` and `inbox_link(public_url, project_id)`, the only builders of links in messages.

**Notes:**
- Secrets are write-only. No response, log or error repeats a webhook URL or password; `GET` returns only `webhook_set`/`password_set`. Enabling a channel that is not set up (no webhook, no host) is a 422.
- The webhook URL and `public_url` are validated with `base_url_problem`. `public_url` must also be `https` with no path, query or userinfo. The webhook must be `https` except for a loopback host (the fake).
- Test sends one fixed `Message` straight through the channel. It writes no log row and no event, and returns the scrubbed `ChannelError` text as `detail`.
- In `notify`, `link` comes from `public_url` when it is set, else it is `None` and the message carries the full summary. `local_mode_note` is true when the run mode is local (S4 §8): the rendered message then says the link opens only on the app's machine.
- **The heartbeat's host (S4 roadmap change):** `HostCheck` also accepts the host of the stored `public_url`, for `GET /api/health/heartbeat` only. The host check runs on every request, so it reads `IntegrationSettings`' cached host (loaded at startup, replaced by the `PUT`), never the database.

**Tests (stop condition):** each route's round trip. A secret is never in a response or in `repr` of the body. Enabling an unset channel gives 422. A bad or non-https `public_url` gives 422. Test succeeds against the fakes and returns a scrubbed `detail` on failure. A notified message carries the incident link under a set `public_url`, has no link without one, and carries the note in local mode; `incident_link` builds `INCIDENT_PATH`. The heartbeat accepts the `public_url` host after the `PUT` and every other path still refuses it. `openapi.json` is regenerated. Full suite, ruff, format, pyright; CI green.

---

### Task 4: Escalation and the channel-failure signal

**Files:** new `backend/src/mlops_agents/notifications/escalation.py` (`EscalationJob`), `notifications/notifier.py` (the failure signal, `bind_incidents`); `api/services.py` (its own lines: `scheduler.register(...)`, `notifier.bind_incidents(incidents)`); tests.

**Interfaces:**
- Consumes `Incidents.list`/`get`/`raise_signal` (incidents task 1), `Scheduler.register` and `ScheduledJob`, `MonitoringSettings.escalation_timeout_minutes`, tasks 1 and 2.
- Produces `EscalationJob(ScheduledJob)` with `kind = JobKind.ESCALATE`.

**Notes:**
- `OnCallPeople` becomes the only writer of `on_call_people`: the notifier reads contacts from `OnCallPeople.list` and its `_save_person_contacts` upsert is removed (the #546/#549 row in `docs/known-debt.md`, removed when this merges). A test that a removed person is not re-created by a retry.
- **Escalation** (S4 §7) applies to an incident that is `critical`, `open` or `reopened`, not acknowledged and not snoozed at `now`, and that has at least one `incident` or `escalation` row. It is due at the last such row plus `escalation_timeout_minutes`; `due` returns one `JobDue` per incident (subject: the incident ID). `run` re-reads the incident and does nothing when it was acknowledged, snoozed, resolved or closed in the meantime. The next person is the first one, in `OnCallPeople.list` order, not yet sent an `incident` or `escalation` row for this incident. That person gets the step 0 row's `Message` again, with `urgent` set, the current cause and `diagnosis_pending = cause is None`, as kind `escalation`. Past the last person nothing more is sent: the incident stays in the Inbox and the morning report. A re-run of the same due pages no one twice (the rows are the record), so catching up after downtime never repeats a page.
- Step 0 and every other immediate delivery are the incidents reactor's (incidents task 4). This plan sends no severity-rise message and registers no `IncidentObserver`: the job reads the log.
- **The channel-failure signal** (S4 §8, §13): after `FAILURE_SIGNAL_AFTER` consecutive deliveries ended `failed` on one channel, the notifier raises `SignalDraft(kind="notification_failure", category="operational", subject="channel:<name>", model=None, …)` through `Incidents.raise_signal`. The cause key is one per channel, so repeats attach under the quiet period instead of paging about the failing pager in a loop. A failure while delivering that incident's own notifications raises nothing more.
- The incidents reactor holds the notifier and the notifier now needs `Incidents`: the notifier is constructed first and gets the incidents service with `bind_incidents(incidents)` once that exists, as `services.py` already does with `bind` for the status board and wake-ups. The constructor never takes it.

**Tests (stop condition):** on the fake clock, a critical incident left unacknowledged pages the second person after the timeout and the third after another. An acknowledgement before the timeout stops it. A snoozed incident is not escalated. A re-run of `run` for the same due sends nothing. After a downtime gap, catching up sends one escalation, not one per missed minute. Past the last person nothing is sent. Three failed deliveries on Discord raise one `notification_failure` signal, a fourth attaches to the same incident, and email is unaffected. Full suite, ruff, format, pyright; CI green.

---

### Task 5: The morning report

**Files:** new `backend/src/mlops_agents/notifications/report.py` (`SqliteMorningReports`, `MorningReportJob`, `report_message`); `api/services.py` (its own lines); tests.

**Interfaces:**
- Consumes `Incidents.list` (incidents task 1), `Expectations.states` (results task 5), `Hooks.runs` (hooks task 1), `next_occurrences` and `Scheduler.register` (results task 1), `Approvals.pending`, `EventLog.page` (types `incident.hold_recorded`, `incident.wake_skipped`), tasks 1 and 2.
- Produces `SqliteMorningReports(MorningReports)` and `MorningReportJob(ScheduledJob)` with `kind = JobKind.MORNING_REPORT`.

**Notes:**
- **When.** The report goes at the start of the person's on-call hours, on their days, in their time zone. That is a cron cadence built from `OnCallHours`, with `next_occurrences` handling DST. `due` gives `JobDue(subject=<person ID>)`. After downtime only the latest missed occurrence per person is sent.
- **`since`** is the person's last `report` row that was sent, else 24 hours before `now`.
- **The body.** It holds:
  - `incidents`: those detected since `since` (all pages of `Incidents.list`), `merged` ones left out;
  - `resolved_offline`: those of them with `resolved_offline`;
  - `actions`: each hook run since `since` (`Hooks.runs`, kinds other than `hold`), with its basis and ref (`none` becomes `bypass`), and each `incident.hold_recorded` event (basis `tier1`);
  - `waiting`: one line per pending approval (agent and tool or proposal kind);
  - `suspended`: the keys whose state is `suspended`;
  - `wakes_skipped`: one line per `incident.wake_skipped` event since `since` (decided point 10).

  Event payloads are read as data. Their text reaches the person, not an agent.
- `report_message(body)` is the one renderer of a report into a `Message`: one `lines` entry per incident (title, severity, status). Its severity is the highest of the listed incidents, `low` when there are none, and it is never `urgent`. The link is `inbox_link`. Sent with `Notifier.notify(kind="report", incident_id=None)`, it records `report.sent`. An empty report (nothing in any part) is not sent.

- **Catch-up (2026-10-30, #540):** the scheduler's catch-up cursor is per job kind, so the morning report's `due()` works out each person's missed report itself from `notification_log` (a person whose report for a day was never sent gets it once), not from the scheduler's `after`.

**Tests (stop condition):** a fake night with a critical incident, a high one outside hours, a rule-backed rollback (a `Hooks.runs` row), a hold, a pending approval, a suspended expectation, an incident resolved offline and a skipped wake. The person's report lists each once, with the action's basis. A second run for the same due sends nothing. Reports follow the person's time zone across a DST change. Two people with different hours get their own report at their own time. An empty night sends nothing. Full suite, ruff, format, pyright; CI green.

---

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
