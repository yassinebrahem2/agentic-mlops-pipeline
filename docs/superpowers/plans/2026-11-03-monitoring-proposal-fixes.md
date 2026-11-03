# Monitoring and Proposal Fixes (walkthrough 3) Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. **Every task is its own early PR** (`AGENTS.md` rule 5): work it on `plan/monitoring-proposal-fixes-task<n>`, created from `origin/main`, and open its PR into `main` when done (`Closes #<task issue>`). There is no plan branch and no plan PR: the plan issue closes when its last task's PR merges. Tasks are independent: do them in any order, each from `origin/main`. Paths are under `backend/src/mlops_agents/` unless they start with `frontend/`.

**Goal:** fix what kept the app from reading and showing the team's monitoring in the lead's walkthrough 3 (2026-11-03; lead's fix list items 19, 20, 21, 22, 26, 28, 29, 30, 31, 33): a parser attached to a made-up source and nothing said so (task 1's validation and a fresh run close 29, the leftover parser); the approval card showed records the parser does not produce, and a mapping could not give each check its own category; a source with no parser was skipped silently and nobody told Onboarding to propose one; the handoff pointed a job feed at a parquet table and the results location at the team's own folder; the pipeline map stayed "unknown"; expectations nobody had judged showed "ok"; the team's fired checks never reached the Dashboard; RegisterCheck took an issue number for a PR; a hook was approved for a command that existed only in an unmerged PR.

**Branches:** one per task, `plan/monitoring-proposal-fixes-task<n>`. **Milestone:** Stage 4: Monitoring loop. **Consumes:** merged code. **Produces:** for the frontend plan, task 6's `ExpectationState.status` value `"not_judged"` and `DashboardView.team_alerts`.

**Review bar:** the lowered bar (`docs/reviewing-and-merging.md`).

## Tasks

| Task | Title | Fix |
|---|---|---|
| 1 | A parser names a source the handoff reads | 26 |
| 2 | A parser's sample records are what it produces | 28 |
| 3 | A source waiting for a parser says so, and Onboarding is told | 19 |
| 4 | The handoff refuses a feed or results location the app can't read | 20 |
| 5 | The pipeline map finds a stage through its tables | 21 |
| 6 | The Dashboard API: "not judged yet" and the team's fired checks | 22, 30 |
| 7 | RegisterCheck takes only the agent's own open PR | 31 |
| 8 | A hook waits for the PR that changes its files | 33 |

---

### Task 1: A parser names a source the handoff reads

**Files:** `monitoring/results/service.py` (make `_derive_sources` public as `sources(project_id) -> list[tuple[str, SourceKind, bool]]`, used by the collector and here); `monitoring/results/parser_kind.py` (`check`); `monitoring/results/contract.py` (`ParserSpec.source_id` gets a `Field` description); `api/services.py` (pass the results service to `ParserKind`); tests in `tests/monitoring/results/`, and every fixture where a team check's file results were read without a parser (search `tests/` for handoffs whose `checks` use `FileResults`, e.g. `tests/proposals/samples.py`, `tests/onboarding/`, `tests/e2e/`): give them a parser or expect the waiting state.

**Notes:**
- `ParserKind.check` refuses a `source_id` that is not one of the project's sources with `needs_parser` true, and the refusal lists those ids ("Propose the parser for one of these source ids, exactly as written: …"; "This project has no source that needs a parser." when none).
- `ParserSpec.source_id` description: the id of a results source exactly as the app names it (`file:<connection>:<pattern>`, `ci:<workflow>`, …); a refusal lists the valid ones.
- The file results of the handoff's own `checks` (the team's existing monitoring, in the team's format) become sources that need a parser in `sources` (today `_add_source(s_id, "file", False)`, ~service.py:249); the results location and agents' registered checks keep reading standard records without one. Without this, a correct handoff (no job feed sharing the team checks' folder) leaves no source a parser may name.

**Tests:** a made-up id is refused and the message lists the real ids (fails on `main`); the real id passes; the results location's own id (read without a parser) is refused; a handoff whose team check writes to a file folder lists that source as needing a parser.

### Task 2: A parser's sample records are what it produces

**Files:** `monitoring/results/parser_kind.py` (`check`); `monitoring/results/contract.py` (`ParserMapping.categories`); `monitoring/results/mapping.py`; `frontend/src/pages/agents/proposals/ParserProposalCard.tsx` (shows `categories`, so the card shows what the parser does) and `frontend/openapi.json` regenerated; tests.

**Notes:**
- `ParserMapping` gains `categories: dict[str, ResultCategory] = {}`, keyed by check id, applied after the field mapping and before `defaults` (a check not listed takes the default). Walkthrough 3's team monitoring mixes `drift` and label-based checks in one file; without it the agent must file drift as `model_performance`, which the app reads as label evidence.
- For a mapping parser, `check` runs `evaluate_mapping(spec.mapping, payload.sample_input, "sample")` (`monitoring/results/mapping.py:221`, sync and pure) and refuses when it reports errors or when its records differ from `sample_records` in count or, record by record, in `check_id`, `fired`, `value`, `threshold`, `category`, `subject` and `model`. The refusal shows what the parser produced, so the agent can fix the mapping or the samples. The approval card then shows records the parser really produces.
- A code parser cannot run on sample text today (`code_parser.py` re-reads the mounted file). Its card says, in the preview body: "The sample records were written by the agent; the app did not run this parser on the sample." Record that in Known debt.

**Tests:** samples that disagree with the mapping (walkthrough 3: `drift` stated as `drift`, mapped as `model_performance`) are refused, naming the field (fails on `main`); with `categories: {"drift": "drift"}` the same samples pass and the collector stores drift as `drift`; a sample the mapping can't parse is refused with its error.

### Task 3: A source waiting for a parser says so, and Onboarding is told

**Files:** `monitoring/results/service.py` (~363-377); `onboarding/kickoff.py`; tests.

**Notes:**
- When a source needs a parser and has none enabled, save its state with `last_error = "Waiting for a parser: propose one with ProposeParser for source <id>."` instead of `None`, every collection, so the Integrations or Checks views and `MonitoringQuery` show why it reads nothing. The error clears when a parser exists.
- `Kickoff.start` also wakes Onboarding when the approved handoff has sources that need a parser (`ResultsService.sources`, task 1's method; this task may land first and keep the private name: use whichever is on `main`): a system message listing each source id, saying "Propose a parser for each with ProposeParser, using the source id exactly as written, test it on a real sample, then check with MonitoringQuery that its results arrive (they are read within a minute) and tell the person what the Dashboard shows." The text lives in `kickoff.py` (`briefs.py` belongs to the definitions plan).

The waiting text must never raise a `source_failure` signal (those come from a failed read, `service.py:~400`, not from the stored state; keep it that way).

**Tests:** a source with no parser stores the waiting error and keeps it each cycle (fails on `main`), and raises no signal; a parser clears it; an approved handoff with such a source posts one message to Onboarding listing it; a handoff with none posts nothing to Onboarding.

### Task 4: The handoff refuses a feed or results location the app can't read

**Files:** `onboarding/handoff_kind.py` (`check`, and `warnings`); `onboarding/handoff_check.py`; tests.

**Notes:** these move from apply time to propose time, so the agent fixes them before the person sees the card:
- A `job` feed whose `file` pattern has an extension no mapping format reads (`jsonl`, `json`, `csv`, `log`) is refused: "a job feed reads result lines; for a table that arrives each day use a data feed (`connection`, `table`)".
- A `results_location` equal to the file source of one of the handoff's own `checks` (the team's existing monitoring) is refused: "the results location is where the agents' own checks write; the team's results are read through their checks' sources".
- The table and connection checks `handoff_check.check` makes today also run at propose time (they stay at apply too).
- A pipeline stage that no feed, check, model or data role can report on (task 5's matching; until task 5 lands, exact names) is a card warning, not a refusal.

**Tests:** each refusal on a walkthrough-3-shaped handoff (job feed on `bookings/*.parquet`; results location = the team checks' folder) (fails on `main`); a valid handoff passes; the stage warning appears on the card.

### Task 5: The pipeline map finds a stage through its tables

**Files:** `monitoring/dashboard.py` (`_stages`, `_has_results`, a map-only matcher); tests.

**Notes:** the map gets its own matcher in `dashboard.py`; `stages_of` (`incidents/contract.py`) stays as it is, because incident grouping and severity use it and wider matching would merge incidents with different causes. The map's matcher is `stages_of` plus a stage whose inputs or outputs hold:
- the data feed table, or the file pattern's top folder, of a feed named by the subject;
- the file results' top folder of a check named by the subject;
- the predictions or labels table of a model named by the subject.
`_stages` uses it for incident signals, expectations and `_has_results`.

**Tests:** walkthrough 3's handoff (stages listing `bookings`, `predictions`, `monitoring`; feeds `daily-scoring` on `bookings/*.parquet`; checks under `monitoring/*.jsonl`; model `hotel-cancellation` with `predictions`) gives no `unknown` stage once results exist (fails on `main`); an unrelated subject matches nothing.

### Task 6: The Dashboard API: "not judged yet" and the team's fired checks

**Files:** `monitoring/expectations/contract.py` (`ExpectationState.status` gains `"not_judged"`); `monitoring/expectations/service.py` (`states`); `monitoring/contract.py` (`DashboardView.team_alerts: list[SilentTeamAlert]`); `monitoring/dashboard.py`; `frontend/openapi.json` regenerated; tests.

**Notes:**
- A recurring expectation with `last_seen` null and no signal yet is `not_judged`, not `ok`, until its first deadline passes (then the current late and silent rules apply).
- `team_alerts`: the team's fired checks the agents stayed silent on (`SqliteBaseline.silent_alerts`), newest first, at most 10.
- **Produces** both for the frontend plan, which shows them (the frontend tolerates the new status value until then).

**Tests:** a fresh expectation is `not_judged`, then `late`/`silent` after its deadline with nothing seen (fails on `main`); `team_alerts` lists a fired team result.

### Task 7: RegisterCheck takes only the agent's own open PR

**Files:** `agents/tools/register_check.py`; tests.

**Notes:** refuse when `pr_number` is not the PR of one of the calling agent's open tasks (`Tasks.open_tasks`, `TaskView.pr_number`, `agent`): "pr_number must be the PR that delivers this check; open it with OpenPullRequest first". Refuse a `check_id` that is one of the approved handoff's team checks: "this check is the team's; register only checks you deliver". Walkthrough 3: Deployment registered the team's three checks with issue #1.

**Tests:** an issue number or another agent's PR is refused (fails on `main`); a team check id is refused; the agent's own open PR passes.

### Task 8: A hook waits for the PR that changes its files

**Files:** `monitoring/hooks/kind.py` (a `check(project_id, payload)` refusal, as `ProposalCheck`); tests.

**Notes:** refuse a hooks proposal when one of a hook's entry files (the workflow file and `entry_files`, `hooks/pins.py:31`) is claimed by an open agent task (`file_claims` of tasks `active` or `in_review`): "<file> is being changed in task <id> (PR #<n>); propose this hook after that PR merges". Walkthrough 3: Deployment proposed `hold-predictions` for a `hold` command that existed only in its unmerged PR #17, and it was approved and pinned to code without it.

**Tests:** a hook whose entry file an open task claims is refused, naming the task and PR (fails on `main`); with no claim it passes.

## Known debt added

- Task 2: code parsers' sample records are the agent's, not run by the app (needs a code-parser run on stdin text).
