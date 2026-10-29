"""The `demo-world` command line."""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pandas as pd

from demo_world import backfill, company, fetch, github, newrun, paths, scenarios, simulate, source


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


def _simulate(args: argparse.Namespace) -> None:
    scenario_path: Path | None = args.scenario
    # Read the scenario and the company setting first, so they fail before the source loads.
    scenario = scenarios.NO_SCENARIO if scenario_path is None else scenarios.load(scenario_path)
    the_company = _company()
    world = simulate.World(
        _loaded_source(), paths.data_dir(), paths.world_dir(), scenario, the_company
    )
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
    simulate_command = commands.add_parser(
        "simulate",
        parents=[run],
        help="play the business days one by one, resuming after the last day done",
    )
    simulate_command.add_argument(
        "--start", type=date.fromisoformat, required=True, help="first day, unless resuming"
    )
    simulate_command.add_argument(
        "--end", type=date.fromisoformat, required=True, help="last day, included"
    )
    simulate_command.add_argument(
        "--seconds-per-day", type=_seconds, required=True, help="pace; 0 runs as fast as it can"
    )
    simulate_command.add_argument(
        "--scenario", type=Path, help="TOML file of injected problems; none are injected without it"
    )
    simulate_command.set_defaults(run_command=_simulate)
    return parser


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
    ) as error:
        print(f"demo-world: {error}", file=sys.stderr)
        return 1
    return 0
