# Stage 4: An Open Incident Keeps Its Cause Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Two tasks in order, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** bug #694. A check that runs daily opens a new incident every day it fires, while the first incident on the same cause key is still open: `find_matching_active_incident` (`monitoring/incidents/grouping.py`) joins a same-cause-key incident only when its latest signal is within `grouping_window_minutes` (60) of the new signal. The spec's quiet period says a signal with an open incident's cause key attaches to it, however long it has been open (S4 §5, made explicit on `main` with this plan). Fixing it alone would make the Stage 7 replay worse: nothing resolves incidents there (only an agent resolves, through `IncidentUpdate`, and the replay's stub agents never act), so every later problem on the same cause key would join the first incident forever. The replay therefore plays the agents' resolve step too.

**Branch:** `plan/stage-4-incident-grouping`. **Milestone:** Stage 4: Monitoring loop. **Consumes:** merged code. **Produces:** the grouping rule as S4 §5 now states it; `grouping_window_minutes` keeps only its baseline use (a team check's firing matches an incident within it, `verdicts/baseline.py`). The plan PR says `Closes` the plan issue, its task issues and #694.

**Read first:** bug #694; S4 §5 "Grouping" and "Quiet period" (`docs/superpowers/specs/2026-10-28-stage-4-monitoring-design.md`); `monitoring/incidents/grouping.py` (`find_matching_active_incident`, `find_reopen_candidate`), `incidents/service.py` (`raise_signal`, `update`'s resolve guard); `bench/benchmarks/src/benchmarks/replay.py` (the day loop) and the Stage 7 spec's Replay paragraph.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | A signal joins the open incident with its cause key | merged code |
| 2 | The replay resolves incidents whose results stopped firing | task 1 |

---

### Task 1: A signal joins the open incident with its cause key

**Files:** `backend/src/mlops_agents/monitoring/incidents/grouping.py`, `incidents/service.py` (the call); `frontend/src/pages/project-settings/MonitoringSection.tsx` (the grouping window's help text says what it now governs: how close a team check's firing must be to an incident's signal to count as the team having fired on it); tests under `backend/tests/monitoring/incidents/`.

**Notes:**
- A signal joins an incident in `ACTIVE_STATUSES` that has its cause key, whatever the time since that incident's last signal (oldest first, as today); the downstream rule is unchanged; a resolved incident is still reopened within `reopen_window_hours`, or a new one opens, as today.
- Grouping no longer takes `grouping_window_minutes`; the setting stays (its baseline use).

**Tests (stop condition):** first, one that fails on `main`: three signals on one cause key a day apart, the incident still open, give one incident with three signals and one wake-up of its agent (no second notification); a signal on another cause key still opens its own incident; reopen within the window and a new incident after it, as today; downstream grouping unchanged; backend suite, ruff, pyright; frontend tests and lint; CI green.

### Task 2: The replay resolves incidents whose results stopped firing

**Files:** `bench/benchmarks/src/benchmarks/replay.py`; `bench/benchmarks/tests/test_replay.py` and `tests/fixture_dataset.py`; `docs` none (the Stage 7 spec says it, on `main` with this plan).

**Notes:**
- After each day's `run_due`, the replay asks the incident service to resolve every active incident, as an agent's `IncidentUpdate` would (`IncidentService.update` with status `resolved`, author `replay`); the service's own guard refuses an incident whose latest results still fire (`incident_still_firing`), which the replay skips; any other error stops the run. Nothing else changes: verdicts and closing stay the scorer's.
- The datasets space problems that share a cause key by at least 9 days, far past `reopen_window_hours` (24), so a later problem opens its own incident.

**Tests (stop condition):** with the fixture dataset: a 3-business-day problem gives exactly one incident, resolved after the day its results stop firing; two problems on the same cause key 9 days apart give two incidents; an incident still firing at the end of the run stays open; `uv run pytest`, ruff, ruff format, pyright in `bench/benchmarks`; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
