# demo-world

The world around the demo company, a developer tool outside the product. The company (the demo repository `yassinebrahem2/hotel-cancellation-demo`) scores hotel bookings for cancellation; `demo-world` plays everything around it: it downloads the source data, gives the company its history, plays time day by day, injects problems at known dates and keeps the evaluation's answer key, and runs the company's scheduled jobs each day. The two share no code: the data folder layout and the company's CLI in `docs/superpowers/plans/2026-10-27-demo-overview.md` are their interface.

## Use

Its own uv project; run from `bench/demo-world/`:

```
uv sync
uv run demo-world fetch                       # download the source CSV once, checked against its pinned SHA-256
uv run demo-world new-run rehearsal           # a run: its own copy of the demo repository and its own folders
uv run demo-world backfill --run rehearsal --until 2016-07-01   # the history, into the run's empty data folder
uv run demo-world up --run rehearsal --start 2016-07-01 --end 2017-03-31 --seconds-per-day 120 --scenario scenarios/default.toml
uv run pytest
uv run ruff check
uv run ruff format --check
```

Settings, each an environment variable; the folders default under `~/hotel-demo/` (for development only: a run has its own, see [Runs](#runs)):

| Variable | Default | Holds |
|---|---|---|
| `HOTEL_DEMO_SOURCE` | `source/` | the downloaded `hotels.csv`, shared by every run |
| `HOTEL_DEMO_DATA` | `data/` | the company's production data: `bookings/` and `labels/`, one parquet file per business date |
| `HOTEL_DEMO_WORLD` | `world/` | the world's own folder, never inside the data folder: `state.json`, `answer_key.jsonl`, `planted_flaws.json`, `hold` (only while held), and with a repository `ci_dispatches.jsonl`, `pull_requests.json`, `ci_failure.json` (only while a CI failure is in place) and, under `up`, `mlflow-server.log` |
| `HOTEL_DEMO_MLFLOW` | `mlflow/` | the company's MLflow store, passed to every company command |
| `HOTEL_DEMO_COMPANY` | none | a checkout of the demo repository; without it `simulate` writes the files only |
| `HOTEL_DEMO_REPO` | none | the company's repository on GitHub (`owner/name`), a run's own copy; without it the world touches nothing on GitHub |
| `HOTEL_DEMO_MLFLOW_PORT` | `5000` | the port of the company's MLflow server under `up` |

`backfill` writes the bookings of every booking date before `--until` (long-lead bookings made before the first arrivals included) and the labels of every release date before it. A label is released on the cancellation date if the booking was cancelled, otherwise on the arrival date, and never before the booking date. It refuses a data folder that already has files.

A backfill that failed midway leaves a non-empty data folder, which the next run refuses: delete that folder and run `backfill` again.

`simulate` plays every calendar day from `--start` to `--end` (both included) as a business date, after the history: each day it writes that day's bookings file, then that day's labels file (the labels released that day); a day with no rows gets no file. `--seconds-per-day` paces it (each day starts that many seconds after the previous one did; 0 runs as fast as it can). `state.json` records the first day played and the last day done, written once the whole day is; a stopped or crashed run started again resumes the day after it and replays a half-done day, writing the world's files identically (the company's own `jobs` and `monitoring` lines of a replayed job are appended again). A `--start` after that day is refused, since it would skip days.

With `HOTEL_DEMO_COMPANY` set, the world runs the company as its ops host would, from the company's own environment (`uv run --locked --project <checkout> hotel-demo ...`, with `HOTEL_DEMO_DATA` and `HOTEL_DEMO_MLFLOW` set to the world's folders, made absolute, and `MLFLOW_TRACKING_URI` and the other variables passed through):
- before each day, `git pull --ff-only` in the checkout, so merged PRs take effect; a failing pull is logged and the day goes on with what is checked out;
- before the first day's files, if the company has no `production` model, `train --cutoff <first day>`, then `evaluate` and `deploy` of the version `train` prints last. Whether one exists is asked of MLflow from the company's environment (`mlflow artifacts download` of `models:/hotel-cancellation@production`, against `MLFLOW_TRACKING_URI` or the company's documented default store); when MLflow cannot say, nothing is trained;
- after the day's files, every job of the checkout's `ops/schedule.toml` due that day, in file order, with `{date}` in its command replaced by the day. A job is due every `every` business days counted from the first day the world played. The file is read again each day, so a job added between days runs from its next due day.

A failing command is logged (command, exit code, the end of its output) and the loop goes on; the log goes to stderr. The company's commands run with `uv run --locked`: when a merged change leaves `uv.lock` out of date with `pyproject.toml`, every company command fails, logged, until a matching lock is merged. The checkout is never rewritten, so `git pull --ff-only` keeps working.

`--scenario` names a TOML file of problems to inject; without it nothing is injected. `scenarios/default.toml` holds one problem of each kind, those that break files or jobs spread over August to October 2016 (July, the first simulated month, is the quiet baseline), `model_decay` over the arrivals of October 2016 to the end of the run (March 2017), and the natural shift the evaluation scores. Each `[[injection]]` has a `kind`, a `start` and an `end` (business dates, both included; arrival dates for `model_decay`), and changes the bookings files dated in that window:

| `kind` | Keys | Effect |
|---|---|---|
| `null_spike` | `columns`, `share` (above 0, at most 1) | sets that share of each day's rows to null in each column, rounded up |
| `schema_break` | `column`, and `rename_to` or `drop = true` | renames or drops the column |
| `out_of_range` | `column` (a float column), `factor` | multiplies the column |
| `score_failure` | none | with a company, the day's files are delivered after its jobs ran, so its scoring fails on a missing bookings file |
| `ci_failure` | none | with a repository, the CI's sample data points at a missing file (see [GitHub](#the-companys-repository-on-github)); without one, recorded in the answer key only, with a warning |
| `model_decay` | `group` (a table of text columns and their values), `max_share` (above 0, at most 1), `ramp_end` (after `start`, at most `end`) | changes no bookings file: it changes the outcomes (labels) of the group's bookings arriving in the window, see below |

Injections that change files must not overlap. Rows are picked from a digest of the kind, column, day and booking, so a rerun writes the same files. Each `[[natural_shift]]` (`columns`, `start`, `end`) is not injected, only listed for scoring.

`model_decay` makes the production model decay while the company's checks stay quiet. In the source, long lead times cancel more, and the first model learned it. For the bookings of the `group` (by default `market_segment = "Online TA"` and `deposit_type = "No Deposit"`) arriving in the window, a share of them start to follow a rule that runs against it: below a lead-time cut they cancel, at or above it they do not, whatever their outcome in the source (both sides change). The rule reads only the booking's own columns, never a model's score. The share ramps with the arrival date from 0 at `start` to `max_share` at `ramp_end`, then holds at `max_share` to `end`, so the decay lasts until someone retrains, and a retrain with the company's own pipeline recovers it only partly (Decision #627); a booking follows the rule when a digest of its ID falls below its arrival day's share, so a rerun picks the same bookings. The cut is computed from the data: over the bookings the rule may change, with the followers' outcomes set by the rule and the others' as in the source, it is the lead time that brings their cancellation rate closest to the group's rate over its bookings arriving before the window (the smallest on a tie). A booking turned into a cancellation is cancelled on a day drawn from a digest of its ID between its booking date and its arrival (the day itself for a booking made for that day); one no longer cancelled is labelled on its arrival; the others keep their label dates. So every label is still released once, never before its booking. Only bookings made on or after the first day the world played are changed: earlier ones are history `backfill` already wrote. The outcomes are set once, when `simulate` starts, before any day's labels are built, so a resumed run releases the same labels. Only labels change: the bookings, which the company scores and its drift check reads, stay as they are, and the overall cancellation rate holds, so the accuracy check has little to see while the model's ranking (PR-AUC) falls. The rule is structured, so a model could learn it in principle (a score that knows the rule reaches 0.885 on February), as random label noise could not be learned; the company's own `train` does not carry it far (Decision #627). Its answer-key line names every column the shift lives in, `market_segment,deposit_type,lead_time`, with the category `model_performance`.

When the loop reaches a problem's first day in the run, it appends one line per column (or one with `column` null) to `answer_key.jsonl`: `{kind, category, column, start, end}`, with `category` one of `data_quality`, `drift`, `model_performance`, `operational`; a line already there is never added again. The key holds two more shapes, told apart by `kind`: the CI failure's real times (`ci_failure_set` and `ci_failure_restored`) and the merger's decisions (`pr_merged` and `pr_not_merged`), both below. `planted_flaws.json` lists the flaws planted in the company's setup by design (`id`, `category`, `where`, `what`), rewritten at every start. Neither is ever written into the data folder, and `simulate` refuses a world folder inside it.

## Runs

No run, evaluation or live demo, touches the demo repository `yassinebrahem2/hotel-cancellation-demo` itself: the world refuses to run when `HOTEL_DEMO_REPO` names it. Each run works on its own public copy (public, so its Actions runs cost no minutes), so every run and every rehearsal starts from the same company, with no data and no model. It needs `gh`, logged in to the account the copies go to.

`demo-world new-run <name>` (lowercase letters, digits and dashes) copies the demo repository, a template, to `<you>/hotel-cancellation-demo-<name>`, clones the copy into `~/hotel-demo/runs/<name>/company`, creates empty `data`, `mlflow` and `world` folders beside it, picks a free port for MLflow (one no other run uses), and writes every setting (`HOTEL_DEMO_REPO`, `HOTEL_DEMO_COMPANY`, `HOTEL_DEMO_DATA`, `HOTEL_DEMO_MLFLOW`, `HOTEL_DEMO_WORLD`, `HOTEL_DEMO_MLFLOW_PORT`) to `~/hotel-demo/runs/<name>/run.env`. It refuses a name already used. `--run <name>` on any other command loads that file; its settings win over the shell's. Two runs share nothing but the source CSV: each bootstraps its own first model.

To delete an old run, delete its copy and its folder:

```
gh repo delete <you>/hotel-cancellation-demo-<name> --yes   # needs the delete_repo scope: gh auth refresh -s delete_repo
```

then remove the folder `~/hotel-demo/runs/<name>` (the run's data, MLflow store, world folder and clone).

## The company's repository on GitHub

With `HOTEL_DEMO_REPO` set (by `--run`), the world also acts on the copy through `gh`. A failing GitHub call is logged (command, exit code, the end of its output) and the loop goes on.

- **CI at simulation speed.** Every `--ci-every N` simulated days (default 7, counted from the first day played) the world dispatches the copy's scheduled workflow (`scheduled.yml`, `workflow_dispatch` on `main`), so the Actions history moves with simulated time. Each dispatch is recorded in `ci_dispatches.jsonl` in the world's folder: `{business_date, dispatched_at, run_id}`, the real time in UTC and the run the dispatch created (found as the newest `workflow_dispatch` run that was not listed before it; `null`, with a warning, when none shows within about 10 seconds). A replayed day is not dispatched again. Nothing of it goes in the answer key or the data folder.
- **`ci_failure`.** On a window's first day, before that day's dispatch, the world points the copy's `SAMPLE_DATA_URL` Actions variable at a file that does not exist, so the workflow's sample-data download fails. On the first day after the window it restores the variable as it was: set back to its value, or deleted when it was unset (the workflow then uses its default). It also restores it on every exit of the loop (the end, Ctrl+C, an error), and a resumed run inside the window breaks it again. The original value is written to `ci_failure.json` first, so a run killed outright is repaired by the next run, which restores it from that file. Each break and restore adds a line to the answer key: `{kind: ci_failure_set or ci_failure_restored, start, end, date, time, set_at}`, the window in business dates, the event's business date and real UTC time (the CI adapter sees real times), and `set_at` tying a restore to its break. At full speed a 7-day window lasts a few real minutes, so a run dispatched in it may start after the restore and pass: compare the run's start with these real times.
- **The PR merger (evaluation runs only).** With `--merge-after-days N`, each day before the checkout's pull (so a merge takes effect that day), the world lists the copy's open PRs. An agent's PR (head branch `agent/<agent>/<task>`, on the copy itself) is merged (`gh pr merge --merge`) once N simulated days have passed since the world first saw it open (its opening is the business date of that first sighting) and GitHub reports no conflict and green checks (success, neutral or skipped). Checks still running, none reported yet, or a mergeability GitHub is still computing: it waits. A PR from a fork of the public copy is never merged, whatever its branch is called: once merged and pulled, its code would run as you. It is recorded once as not merged, with the reason `cross_repository`. A conflict, a failed check or a failed merge is recorded as not merged and left for the agents; the world tries again each day and records a refusal again only when its reason changes. Each decision goes in the answer key: `{kind: pr_merged or pr_not_merged, pr, branch, opened, date, delay_days, merge_after_days, reason}`, `reason` one of `conflict`, `checks_failed`, `merge_failed`, `cross_repository` (null for a merge). The world keeps the first sightings in `pull_requests.json`. Without the option the world never merges: in a live demo the user does.

`--ci-every` and `--merge-after-days` are refused without `HOTEL_DEMO_REPO`.

## `up`

`demo-world up` takes the options of `simulate`. It first runs `mlflow --version` from the company's environment, which in a fresh clone builds that environment (this can take minutes; a failure stops `up` with MLflow's output). Then it starts the company's MLflow server from that environment (`uv run --locked --project <checkout> mlflow server`) on the company's store (`sqlite:///<HOTEL_DEMO_MLFLOW>/mlflow.db`, artifacts in `<HOTEL_DEMO_MLFLOW>/artifacts`), at `127.0.0.1:HOTEL_DEMO_MLFLOW_PORT`, its output in the world's `mlflow-server.log`. Once the server answers `/health` (within 2 minutes, or `up` stops it with an error), it sets `MLFLOW_TRACKING_URI` to it and runs `simulate`. Ctrl+C stops both, on Windows too: the server runs in a process group of its own, which the world stops, with every process it started, on every exit. On Windows that is a console process group asked to stop with `CTRL_BREAK_EVENT`, inside a job object that is then terminated; the system also terminates the job if the world is killed outright. Elsewhere it is a new session, sent `SIGTERM`, then `SIGKILL`; the `SIGKILL` is sent even if a second Ctrl+C interrupts the wait. While `up` runs, `SIGTERM` and `SIGHUP` sent to the world (`kill <pid>`, a closed terminal) stop it the same way, with exit code 128 plus the signal's number. A run stopped this way resumes with the same command. Only a `SIGKILL` of the world itself leaves the server running on Linux and macOS; stop it then with `kill -- -<server pid>`.

When a marker file `hold` exists in the world folder, the day loop checks before each day: while it exists, the loop waits (MLflow stays served, nothing is written, no jobs run, no simulated time advances). Once `hold` is removed, the day loop resumes; the `--seconds-per-day` pace restarts from the resume (a hold is not a day). A held run that is stopped and started again resumes as today.

With `--hold-before-start`, `up` creates the `hold` file after the first model is trained and deployed and the history is backfilled, before the first simulated day. MLflow serves while held, allowing agents or the user to inspect the first model and history before simulation begins.

```
uv run demo-world up --run rehearsal --start 2016-07-01 --end 2017-03-31 --seconds-per-day 120 --scenario scenarios/default.toml
```

## A live demo

A live demo uses its own run too, without `--merge-after-days` (the user merges). The full span (274 days) at 120 seconds a day takes about 9 hours, so fast-forward to the day the demo starts, then play on at a slower pace; `state.json` resumes where the fast part stopped:

```
uv run demo-world new-run live
uv run demo-world backfill --run live --until 2016-07-01
uv run demo-world up --run live --start 2016-07-01 --end 2016-07-31 --seconds-per-day 0 --scenario scenarios/default.toml
uv run demo-world up --run live --start 2016-07-01 --end 2017-03-31 --seconds-per-day 120 --scenario scenarios/default.toml
```

## Data

Hotel Booking Demand, by Nuno Antonio, Ana de Almeida and Luis Nunes, *Data in Brief* 22 (2019), [doi:10.1016/j.dib.2018.11.126](https://doi.org/10.1016/j.dib.2018.11.126), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). `fetch` downloads the copy published by TidyTuesday (2020-02-11) from a URL pinned to one commit of `rfordatascience/tidytuesday`. The data is re-sliced, not redistributed as is: the world converts its missing-value markers to nulls, derives booking and release dates, and splits it into one file per business date. Neither this repository nor the demo repository commits the CSV.
