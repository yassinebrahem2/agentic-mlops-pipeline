# Stage 7: Developer Benchmarks — Plans Overview

**Spec:** `docs/superpowers/specs/2026-11-01-stage-7-benchmarks-design.md`. **Milestone:** Stage 7: Developer benchmarks. **Deadline:** 2026-11-05.

**Goal.** A developer tool, `bench/benchmarks/`, that scores the app against known problems through the product's own verdict routes and reads the numbers back from its Stats: replays of three datasets without a model, and one short real-agent run on the demo world.

## Global constraints

- **Outside the product.** `bench/benchmarks/` is its own uv project (Python 3.12, `>=3.12`, hatchling, ruff line length 100, pyright strict), with path dependencies on `backend/` (`mlops-agents`) and `bench/demo-world/` (`demo-world`). Nothing in `backend/` imports it; it never ships in the wheel.
- **App access.** The scorer and the agents-run command talk to the app only over HTTP (`BenchClient`); only the replay builds the app in-process and sets its project up through `Services`, as the stage 4 end-to-end test does. Nothing reads `app.db` or `project.db`.
- **Secrets.** The access token comes from `MLOPS_AGENTS_ACCESS_TOKEN` only; never logged, printed, put in a URL or written to a report. Provider keys are the person's, entered in the app; the bench never sees them.
- **No metric of its own.** Every number in a report is a Stats value (`StatsView`) or a count the bench recorded (key problems, pauses, usage); the bench computes no precision, recall or delay itself.
- **One home each.** The answer-key line is `demo_world.answer_key.Record`; the agents run's scenario uses demo-world's format and parser (`demo_world.scenarios`), while each replay dataset defines its injections in its own module (demo-world's injections work on the hotel bookings frame) and shares only `Record`; the verdict and window types are the backend's (`VerdictDraft`, `MissReport`, `SilentVerdict`, `WindowSpec`, `StatsView`), imported, never copied.
- **CI.** `.github/workflows/benchmarks.yml`: push-only on `main` and `plan/**`, paths `bench/benchmarks/**`, `bench/demo-world/**`, `backend/src/**` and the workflow itself; Ubuntu and Windows; `uv sync --locked`, ruff check and format, pyright, pytest. Tests run on small fixtures; no dataset download and no model call in CI.
- **Data.** Datasets are fetched at run time from pinned URLs with a pinned SHA-256 into a cache folder outside the repo (`BENCH_HOME`, default `~/bench-home/`); runs write under `BENCH_HOME/runs/<run>/`. Nothing is committed.

## Shared interfaces (exact)

```python
# bench/benchmarks/src/benchmarks/client.py   (core task 1)
class BenchClient(Protocol):
    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Any: ...
    async def post(self, path: str, body: Mapping[str, Any]) -> Any: ...   # sends X-CSRF-Token
async def sign_in(base_url: str) -> BenchClient           # POST /auth/token with MLOPS_AGENTS_ACCESS_TOKEN; every path is a route without /api, the client adds it
def in_process(app: FastAPI, token: str) -> BenchClient   # httpx ASGI transport, same sign-in

# bench/benchmarks/src/benchmarks/dates.py   (core task 1)
type DayStarts = Sequence[tuple[date, datetime]]          # business date -> its start, ascending, UTC
def business_date(at: datetime, starts: DayStarts) -> date   # the last start at or before `at`; before the first: the first date

# bench/benchmarks/src/benchmarks/cli.py   (core task 1)
COMMANDS: list[Command]     # each subcommand is one entry, appended by the task or plan that adds it (one line)

# bench/benchmarks/src/benchmarks/scorer.py   (core task 2)
@dataclass(frozen=True)
class Problem:                                            # one injected problem: Record lines sharing kind, start, end
    kind: str; category: ResultCategory; start: date; end: date; columns: tuple[str | None, ...]
def problems(key: Iterable[Record]) -> list[Problem]      # only Record lines; CiFailureEvent and PullDecision lines skipped
@dataclass(frozen=True)
class Score:
    matched: Mapping[str, Problem]                        # incident ID -> its problem
    verdicts: Mapping[str, VerdictDraft]                  # incident ID -> the verdict posted
    misses: Sequence[Problem]
    silent: Mapping[str, SilentVerdict]                   # silent team alert result ID -> verdict posted
    excluded: Sequence[str]                               # incident IDs left unjudged (merged)
class AlreadyScored(Exception): ...
GRACE_DAYS: Final = 7
async def score(client: BenchClient, project_id: str, key: Sequence[Record], starts: DayStarts) -> Score

# bench/benchmarks/src/benchmarks/report.py   (core task 3)
@dataclass(frozen=True)
class Pause:
    reason: Literal["ready_wait", "model_stall"]; start: datetime; end: datetime
@dataclass(frozen=True)
class RunInfo:
    mode: Literal["replay", "agents"]; dataset: str; scenario: str; seed: int | None; app_commit: str
    started: datetime; ended: datetime; provider: str | None; model: str | None
    seconds_per_day: float | None; pauses: Sequence[Pause]; day_gaps: Sequence[date]
async def write_report(client: BenchClient, project_id: str, run_dir: Path, info: RunInfo, score: Score) -> tuple[Path, Path]
    # reads /stats (window all; overall and per category) and the usage records itself; writes report.md and report.json
# CLI: `bench report <run_dir>...`: one table of several runs' report.json

# bench/benchmarks/src/benchmarks/datasets/contract.py   (core task 4)
class Dataset(Protocol):
    name: str
    def days(self) -> Sequence[date]: ...
    def handoff(self) -> HandoffPayload: ...              # its feeds, its checks with a file results location, its team check
    def prepare(self, cache: Path) -> None: ...           # fetch and verify, or generate; idempotent
    def write_day(self, day: date, data_dir: Path, results_dir: Path) -> None: ...   # that day's files and its check records
    def key(self) -> Sequence[Record]: ...
# bench/benchmarks/src/benchmarks/datasets/__init__.py   (core task 4)
DATASETS: dict[str, Callable[[], Dataset]]   # `bench replay <name>`; each dataset adds its one line
# bench/benchmarks/src/benchmarks/checks.py   (core task 4): the fixed generic checks, each a pure function over frames
#   -> ResultRecord (ran_at = the day's start; origin "agent", so a fire opens an incident, as the stage 4 e2e's agent check in the handoff does): null_share, out_of_range, psi_drift, pr_auc_matured; accuracy (origin "team", the baseline)
# bench/benchmarks/src/benchmarks/replay.py   (core task 4)
async def replay(dataset: Dataset, run_dir: Path) -> Score     # builds the app in-process, plays every day, scores, writes the report
# CLI: `bench replay <dataset> [--seed N]`
```

## Plans

| Plan | Branch | Covers | Consumes | Produces |
|---|---|---|---|---|
| Bench core | `plan/stage-7-bench-core` | the project and CI; `BenchClient`, `DayStarts`; the scorer; the report; the dataset interface, the fixed checks, the replay and its stub model; ULB | `main`; `demo_world.answer_key` (#533 merged) | everything in the block above; early PRs after task 3 (client, dates, scorer, report) and after task 4 (dataset interface, checks, replay) |
| Bench datasets | `plan/stage-7-bench-datasets` | the synthetic drift and the telemetry datasets, each with its replay report | core task 4's early PR | nothing |
| Bench agents run | `plan/stage-7-bench-agents` | demo-world holding its day loop with MLflow served; the short scenario; `day_starts_from_jobs`; the agents-run command; the run and its report | core task 3's early PR; #533 merged; plan #643 merged (demo repo); task #647 merged (`ran_at` text) | `day_starts_from_jobs(jobs_dir: Path) -> tuple[list[tuple[date, datetime]], list[date]]` (starts, gaps), in its own `bench/benchmarks/src/benchmarks/jobs_days.py` |

Both later plans start once their Consumes are on `main`; the datasets and the agents plans run in parallel.

## Review focus

- No number the bench computes itself where Stats has one; no number shown in a mode that cannot fill it (spec, "The report").
- The scorer runs once per project and judges each problem once.
- No secret in a log, a report or an error.
- A pause never raises a silence and never becomes a miss.
