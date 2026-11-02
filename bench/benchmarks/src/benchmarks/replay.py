"""In-process replay runner for benchmark datasets."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

from mlops_agents.api.app import create_app
from mlops_agents.api.services import Services
from mlops_agents.config import RunMode, load_settings
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.results.contract import ParserMapping, ParserSpec, source_id
from mlops_agents.proposals.payloads import DataFeedRef, FileResults, JobFeedRef

from benchmarks.client import in_process
from benchmarks.datasets import DATASETS
from benchmarks.datasets.contract import Dataset
from benchmarks.fake_clock import FakeClock
from benchmarks.report import RunInfo, write_report
from benchmarks.scorer import AlreadyScored, Score, score
from benchmarks.stub_model import register_stub_model, start_stub_model


class _MemoryKeyStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self._data.get(ref)

    def set(self, ref: str, value: str) -> None:
        self._data[ref] = value

    def delete(self, ref: str) -> None:
        self._data.pop(ref, None)


def _init_repo(path: Path) -> Path:
    git_bin = shutil.which("git") or "git"
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "bench",
            "GIT_AUTHOR_EMAIL": "bench@example.com",
            "GIT_COMMITTER_NAME": "bench",
            "GIT_COMMITTER_EMAIL": "bench@example.com",
        }
        subprocess.run(  # noqa: S603
            [git_bin, "init", "-b", "main"],
            cwd=path,
            check=True,
            capture_output=True,
            env=git_env,
        )
        subprocess.run(  # noqa: S603
            [git_bin, "config", "user.name", "bench"],
            cwd=path,
            check=True,
            capture_output=True,
            env=git_env,
        )
        subprocess.run(  # noqa: S603
            [git_bin, "config", "user.email", "bench@example.com"],
            cwd=path,
            check=True,
            capture_output=True,
            env=git_env,
        )
        readme = path / "README.md"
        if not readme.exists():
            readme.write_text("# Bench Replay\n", encoding="utf-8")
        subprocess.run(  # noqa: S603
            [git_bin, "add", "."],
            cwd=path,
            check=True,
            capture_output=True,
            env=git_env,
        )
        subprocess.run(  # noqa: S603
            [git_bin, "commit", "-m", "initial commit"],
            cwd=path,
            check=True,
            capture_output=True,
            env=git_env,
        )
    return path


def _get_app_commit() -> str:
    repo_root = Path(__file__).resolve().parents[4]
    git_bin = shutil.which("git") or "git"
    res = subprocess.run(  # noqa: S603
        [git_bin, "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    commit = res.stdout.strip()[:8]
    if not commit:
        raise RuntimeError("git rev-parse HEAD returned empty commit")
    return commit


def _stamp_day_files_sync(files: Sequence[Path], day_start: datetime) -> None:
    day_ts = day_start.timestamp()
    for file_path in files:
        if file_path.is_file():
            os.utime(file_path, (day_ts, day_ts))


COMMON_RESULTS_MAPPING_FIELDS: Final[dict[str, str]] = {
    "check_id": "check_id",
    "ran_at": "ran_at",
    "value": "value",
    "fired": "fired",
    "subject": "subject",
    "category": "category",
    "detail": "detail",
}


def _make_parser_spec(source_id: str, default_category: str, default_check_id: str) -> ParserSpec:
    return ParserSpec(
        source_id=source_id,
        mapping=ParserMapping(
            format="jsonl",
            fields=COMMON_RESULTS_MAPPING_FIELDS,
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": default_category, "check_id": default_check_id},
        ),
    )


async def replay(
    dataset: Dataset,
    run_dir: Path,
) -> Score:
    """Builds the app in-process, plays every day, scores, and writes report."""
    resolved_run_dir = await asyncio.to_thread(run_dir.resolve)
    report_json_path = resolved_run_dir / "report.json"
    home_dir = resolved_run_dir / "home"
    if await asyncio.to_thread(report_json_path.exists) or await asyncio.to_thread(home_dir.exists):
        raise AlreadyScored(f"Run directory {resolved_run_dir} has already been used/scored")

    run_started = datetime.now(UTC)

    # 1. Prepare dataset cache
    bench_home = Path(os.environ.get("BENCH_HOME", Path.home() / "bench-home"))
    cache_dir = bench_home / "cache"
    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(dataset.prepare, cache_dir)

    # 2. Local git repository
    repo_dir = await asyncio.to_thread(_init_repo, resolved_run_dir / "repo")

    # 3. App environment & clock
    await asyncio.to_thread(home_dir.mkdir, parents=True, exist_ok=True)

    days_seq: Sequence[date] = list(dataset.days())
    if not days_seq:
        raise ValueError("Dataset must have at least one day")

    first_day = days_seq[0]
    initial_clock_dt = datetime(first_day.year, first_day.month, first_day.day, 0, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=initial_clock_dt)

    token = secrets.token_urlsafe(32)
    env = {
        "MLOPS_AGENTS_HOME": str(home_dir),
        "MLOPS_AGENTS_ACCESS_TOKEN": token,
    }
    settings = load_settings(RunMode.LOCAL, port=8765, env=env)
    app = create_app(settings, keystore=_MemoryKeyStore(), clock=clock)
    app.state.access_token = token

    stub = start_stub_model()
    try:
        async with app.router.lifespan_context(app):
            client = in_process(app, token)
            services: Services = app.state.services

            # Stop background scheduler loop
            await services.scheduler.stop()

            # Register stub model & set default for all agents
            model_addr = await register_stub_model(client, stub)
            agent_views = await services.agent_settings.list_settings()
            for av in agent_views:
                await services.agent_settings.set_default_model(av.agent, model_addr)

            # Always fresh project setup
            project = await services.workspace.add_local(str(repo_dir))
            project_id = project.id

            # Set up local folder connection on data folder
            data_dir = resolved_run_dir / "data"
            results_dir = data_dir / "results"
            await asyncio.to_thread(data_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(results_dir.mkdir, parents=True, exist_ok=True)

            handoff = dataset.handoff()

            # Find connection names required by handoff
            conn_names: set[str] = set()
            for role in handoff.data_roles:
                conn_names.add(role.connection)
            if isinstance(handoff.operations.results_location, FileResults):
                conn_names.add(handoff.operations.results_location.connection)
            for feed in handoff.operations.feeds:
                if isinstance(feed.ref, DataFeedRef):
                    conn_names.add(feed.ref.connection)
                elif feed.ref.file is not None:
                    conn_names.add(feed.ref.file.connection)
            for chk in handoff.operations.checks:
                if isinstance(chk.results, FileResults):
                    conn_names.add(chk.results.connection)

            existing_conns = {c.name for c in await services.connections.list(project_id)}
            for cname in conn_names:
                if cname not in existing_conns:
                    await services.connections.create(
                        project_id,
                        kind=LOCAL_FOLDER_KIND,
                        name=cname,
                        settings={"path": str(data_dir)},
                    )

            # Approve handoff
            await services.handoffs.store(project_id, "prp_replay_handoff", handoff)

            # Approve parsers declared by checks and feeds
            for chk in handoff.operations.checks:
                if isinstance(chk.results, FileResults):
                    s_id = source_id("file", chk.results.path_pattern, chk.results.connection)
                    if await services.parsers.current(project_id, s_id) is None:
                        spec = _make_parser_spec(s_id, chk.category, chk.check_id)
                        await services.parsers.store(
                            project_id, spec, proposal_id=f"prp_parser_{chk.check_id}"
                        )

            for feed in handoff.operations.feeds:
                if isinstance(feed.ref, JobFeedRef) and feed.ref.file is not None:
                    s_id = source_id("file", feed.ref.file.path_pattern, feed.ref.file.connection)
                    if await services.parsers.current(project_id, s_id) is None:
                        matched_chk = next(
                            (
                                c
                                for c in handoff.operations.checks
                                if isinstance(c.results, FileResults)
                                and c.results.path_pattern == feed.ref.file.path_pattern
                            ),
                            None,
                        )
                        cat = matched_chk.category if matched_chk else "model_performance"
                        cid = matched_chk.check_id if matched_chk else feed.name
                        spec = _make_parser_spec(s_id, cat, cid)
                        await services.parsers.store(
                            project_id, spec, proposal_id=f"prp_parser_{feed.name}"
                        )

            # Day loop
            day_starts: list[tuple[date, datetime]] = []
            for day in days_seq:
                day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=UTC)
                day_starts.append((day, day_start))

                files_before = {p: p.stat().st_mtime_ns for p in data_dir.rglob("*") if p.is_file()}

                await asyncio.to_thread(dataset.write_day, day, data_dir, results_dir)

                # Stamp only what was written on this day
                written_files = [
                    p
                    for p in data_dir.rglob("*")
                    if p.is_file()
                    and (p not in files_before or p.stat().st_mtime_ns != files_before[p])
                ]
                await asyncio.to_thread(_stamp_day_files_sync, written_files, day_start)

                # Advance clock to day_start + 1 hour
                clock.set(day_start + timedelta(hours=1))

                # Drive due jobs
                await services.scheduler.run_due(project_id, clock.now())
                await asyncio.sleep(0)

            # Scoring
            run_ended = datetime.now(UTC)
            score_res = await score(client, project_id, dataset.key(), day_starts)

            # Write reports
            scenario_name = getattr(
                dataset, "scenario", getattr(dataset, "scenario_name", dataset.name)
            )
            dataset_seed = getattr(dataset, "seed", None)
            info = RunInfo(
                mode="replay",
                dataset=dataset.name,
                scenario=str(scenario_name),
                seed=dataset_seed,
                app_commit=_get_app_commit(),
                started=run_started,
                ended=run_ended,
                provider=None,
                model=None,
                seconds_per_day=None,
                pauses=(),
                day_gaps=(),
            )
            await write_report(client, project_id, resolved_run_dir, info, score_res)
            return score_res
    finally:
        stub.stop()


def register_replay_cli(parser: argparse.ArgumentParser) -> None:
    """Registers CLI arguments for `bench replay`."""
    parser.add_argument("dataset", help="Name of the dataset to replay")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for deterministic runs",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Directory to store run outputs (default: BENCH_HOME/runs/replay-<dataset>)",
    )


def run_replay_cli(args: argparse.Namespace) -> int:
    """Executes the `bench replay` command."""
    dataset_name = str(args.dataset)
    if dataset_name not in DATASETS:
        sys.stderr.write(
            f"Unknown dataset {dataset_name!r}. Available datasets: {sorted(DATASETS.keys())}\n"
        )
        return 1

    factory = DATASETS[dataset_name]
    dataset: Dataset = factory()
    if args.seed is not None and hasattr(dataset, "seed"):
        object.__setattr__(dataset, "seed", args.seed)

    run_dir: Path | None = args.run_dir
    if run_dir is None:
        bench_home = Path(os.environ.get("BENCH_HOME", Path.home() / "bench-home"))
        run_dir = bench_home / "runs" / f"replay-{dataset_name}"

    try:
        score_res = asyncio.run(replay(dataset, run_dir))
        sys.stdout.write(
            f"Replay complete: {len(score_res.matched)} matched, {len(score_res.misses)} misses\n"
        )
        return 0
    except AlreadyScored as exc:
        sys.stderr.write(f"Refused: {exc}\n")
        return 1
