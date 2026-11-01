"""The `demo-world` command line."""

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import pandas as pd

from demo_world import (
    backfill,
    ci,
    ci_failure,
    company,
    fetch,
    github,
    merger,
    newrun,
    paths,
    scenarios,
    simulate,
    source,
    up,
)

log = logging.getLogger(__name__)


class SourceMissingError(Exception):
    """The source CSV has not been fetched yet."""


def _loaded_source() -> pd.DataFrame:
    csv_path = paths.source_dir() / fetch.SOURCE_FILE
    if not csv_path.exists():
        raise SourceMissingError(f"{csv_path} is missing; run `demo-world fetch` first")
    return source.load_source(csv_path)


def _fetch(_: argparse.Namespace) -> None:
    written = fetch.fetch(paths.source_dir(), fetch.SOURCE_URL, fetch.SOURCE_SHA256)
    print(f"source ready: {written}")


def _backfill(args: argparse.Namespace) -> None:
    until: date = args.until
    summary = backfill.backfill(_loaded_source(), paths.data_dir(), until)
    print(
        f"wrote {summary.bookings_files} bookings files and {summary.labels_files} labels files "
        f"before {until.isoformat()} to {paths.data_dir()}"
    )


def _new_run(args: argparse.Namespace) -> None:
    folder = newrun.new_run(args.name, github.RUNNER)
    settings = newrun.load_run(args.name)
    print(
        f"run {args.name} ready in {folder}: the company is {settings[paths.REPO_VARIABLE]}, "
        f"cloned in {settings[paths.COMPANY_VARIABLE]}; use `--run {args.name}` on every command"
    )


def _company() -> company.Company | None:
    checkout = paths.company_dir()
    if checkout is None:
        return None
    if not checkout.is_dir():
        raise company.CompanyError(
            f"{paths.COMPANY_VARIABLE} names {checkout}, which is not a checkout of the demo repository"
        )
    return company.Company.at(checkout, paths.data_dir(), paths.mlflow_dir())


def _steps(args: argparse.Namespace, scenario: scenarios.Scenario) -> tuple[simulate.Step, ...]:
    """The world's work on the company's repository; none without HOTEL_DEMO_REPO."""
    name = paths.repository()
    if name is None:
        for option, given in [
            ("--ci-every", args.ci_every),
            ("--merge-after-days", args.merge_after_days),
        ]:
            if given is not None:
                raise github.GitHubError(
                    f"{option} acts on the company's GitHub repository; it needs "
                    f"{paths.REPO_VARIABLE} (see `demo-world new-run`)"
                )
        if any(p.kind == "ci_failure" for p in scenario.problems):
            log.warning(
                "without %s the ci_failure windows are recorded in the answer key only; "
                "nothing on GitHub changes",
                paths.REPO_VARIABLE,
            )
        return ()
    repository = github.Repository(name, github.RUNNER)
    every: int = ci.DEFAULT_EVERY if args.ci_every is None else args.ci_every
    # The CI failure step runs even without a window: it repairs a variable a killed run left
    # pointing at a missing file. It switches at the start of a day, before that day's dispatch.
    steps: list[simulate.Step] = [
        ci_failure.CiFailure(repository, paths.world_dir(), scenario.problems),
        ci.Dispatcher(repository, paths.world_dir(), every),
    ]
    if args.merge_after_days is not None:
        steps.append(merger.Merger(repository, paths.world_dir(), args.merge_after_days))
    return tuple(steps)


def _world(args: argparse.Namespace) -> simulate.World:
    scenario_path: Path | None = args.scenario
    # Read the scenario and the settings first, so they fail before the source loads.
    scenario = scenarios.NO_SCENARIO if scenario_path is None else scenarios.load(scenario_path)
    the_company = _company()
    steps = _steps(args, scenario)
    return simulate.World(
        _loaded_source(), paths.data_dir(), paths.world_dir(), scenario, the_company, steps
    )


def _simulate(args: argparse.Namespace) -> None:
    _play(_world(args), args)


def _up(args: argparse.Namespace) -> None:
    world = _world(args)
    if world.company is None:
        raise company.CompanyError(
            f"up runs the company's MLflow server from its checkout; set {paths.COMPANY_VARIABLE} "
            "or use --run"
        )
    up.up(
        world.company,
        paths.world_dir(),
        paths.mlflow_port(),
        lambda: _play(world, args),
        hold_before_start=args.hold_before_start,
        cutoff=args.start,
    )


def _play(world: simulate.World, args: argparse.Namespace) -> None:
    end: date = args.end
    summary = simulate.simulate(world, args.start, end, simulate.Pace(args.seconds_per_day))
    if summary.first is None:
        print(f"nothing left to play up to {end.isoformat()}")
        return
    print(
        f"played {summary.first.isoformat()} to {end.isoformat()} ({summary.days} days) "
        f"into {paths.data_dir()}; the world's state is in {paths.world_dir()}"
    )


def _seconds(text: str) -> float:
    seconds = float(text)
    if seconds < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, not {text}")
    return seconds


def _at_least(minimum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        number = int(text)
        if number < minimum:
            raise argparse.ArgumentTypeError(f"must be {minimum} or more, not {text}")
        return number

    return parse


def _run_option() -> argparse.ArgumentParser:
    option = argparse.ArgumentParser(add_help=False)
    option.add_argument(
        "--run",
        metavar="NAME",
        help="use the settings of the run NAME (see new-run) instead of the environment",
    )
    return option


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo-world", description="The world around the demo company."
    )
    run = _run_option()
    commands = parser.add_subparsers(required=True, metavar="command")
    new_run_command = commands.add_parser(
        "new-run", help="create a run: its own copy of the demo repository and its own folders"
    )
    new_run_command.add_argument("name", help="the run's name: lowercase letters, digits, dashes")
    new_run_command.set_defaults(run_command=_new_run)
    fetch_command = commands.add_parser(
        "fetch", parents=[run], help="download the source CSV once into the source folder"
    )
    fetch_command.set_defaults(run_command=_fetch)
    backfill_command = commands.add_parser(
        "backfill",
        parents=[run],
        help="write the history before a date into an empty data folder",
    )
    backfill_command.add_argument(
        "--until", type=date.fromisoformat, required=True, help="first day not in the history"
    )
    backfill_command.set_defaults(run_command=_backfill)
    play = _play_options()
    simulate_command = commands.add_parser(
        "simulate",
        parents=[run, play],
        help="play the business days one by one, resuming after the last day done",
    )
    simulate_command.set_defaults(run_command=_simulate)
    up_command = commands.add_parser(
        "up",
        parents=[run, play],
        help="start the company's MLflow server, then simulate against it; Ctrl+C stops both",
    )
    up_command.add_argument(
        "--hold-before-start",
        action="store_true",
        help="hold the day loop before the first simulated day, with MLflow served",
    )
    up_command.set_defaults(run_command=_up)
    return parser


def _play_options() -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument(
        "--start", type=date.fromisoformat, required=True, help="first day, unless resuming"
    )
    options.add_argument("--end", type=date.fromisoformat, required=True, help="last day, included")
    options.add_argument(
        "--seconds-per-day", type=_seconds, required=True, help="pace; 0 runs as fast as it can"
    )
    options.add_argument(
        "--scenario", type=Path, help="TOML file of injected problems; none are injected without it"
    )
    options.add_argument(
        "--ci-every",
        type=_at_least(1),
        metavar="N",
        help=f"dispatch the company's scheduled CI every N simulated days (default "
        f"{ci.DEFAULT_EVERY}; needs {paths.REPO_VARIABLE})",
    )
    options.add_argument(
        "--merge-after-days",
        type=_at_least(0),
        metavar="N",
        help="evaluation runs only: merge an agent's PR N simulated days after it opened, once "
        f"its checks are green (needs {paths.REPO_VARIABLE}); without it the world never merges",
    )
    return options


def _settle_settings(args: argparse.Namespace) -> None:
    """Load the run's settings, then refuse a repository setting the world may not use."""
    run_name: str | None = getattr(args, "run", None)
    if run_name is not None:
        # The run's settings win over the shell's: a run never mixes in another's folders.
        os.environ.update(newrun.load_run(run_name))
    repository = paths.repository()
    if repository is not None:
        github.checked_repository(repository)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # The company's commands and their failures are reported through logging, on stderr.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        _settle_settings(args)
        args.run_command(args)
    except (
        SourceMissingError,
        fetch.FetchError,
        backfill.BackfillError,
        scenarios.ScenarioError,
        simulate.SimulateError,
        company.CompanyError,
        github.GitHubError,
        newrun.RunError,
        up.UpError,
    ) as error:
        print(f"demo-world: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Everything the world started is stopped by now; a rerun resumes after the last day.
        print("demo-world: stopped", file=sys.stderr)
        return 130
    except up.Terminated as stop:
        # As for Ctrl+C: `up` has stopped the server; the exit code says which signal.
        print(f"demo-world: {stop}", file=sys.stderr)
        return 128 + stop.signal_number
    return 0
