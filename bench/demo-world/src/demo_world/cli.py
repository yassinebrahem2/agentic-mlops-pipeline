"""The `demo-world` command line."""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pandas as pd

from demo_world import backfill, fetch, paths, scenarios, simulate, source


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


def _simulate(args: argparse.Namespace) -> None:
    scenario_path: Path | None = args.scenario
    # Read the scenario first, so a bad file fails before the source is loaded.
    scenario = scenarios.NO_SCENARIO if scenario_path is None else scenarios.load(scenario_path)
    world = simulate.World(_loaded_source(), paths.data_dir(), paths.world_dir(), scenario)
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo-world", description="The world around the demo company."
    )
    commands = parser.add_subparsers(required=True, metavar="command")
    fetch_command = commands.add_parser(
        "fetch", help="download the source CSV once into the source folder"
    )
    fetch_command.set_defaults(run=_fetch)
    backfill_command = commands.add_parser(
        "backfill", help="write the history before a date into an empty data folder"
    )
    backfill_command.add_argument(
        "--until", type=date.fromisoformat, required=True, help="first day not in the history"
    )
    backfill_command.set_defaults(run=_backfill)
    simulate_command = commands.add_parser(
        "simulate", help="play the business days one by one, resuming after the last day done"
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
    simulate_command.set_defaults(run=_simulate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.run(args)
    except (
        SourceMissingError,
        fetch.FetchError,
        backfill.BackfillError,
        scenarios.ScenarioError,
        simulate.SimulateError,
    ) as error:
        print(f"demo-world: {error}", file=sys.stderr)
        return 1
    return 0
