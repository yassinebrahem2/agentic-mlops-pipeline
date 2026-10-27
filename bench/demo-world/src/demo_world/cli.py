"""The `demo-world` command line."""

import argparse
import sys
from collections.abc import Sequence
from datetime import date

from demo_world import backfill, fetch, paths, source


def _fetch(_: argparse.Namespace) -> None:
    written = fetch.fetch(paths.source_dir(), fetch.SOURCE_URL, fetch.SOURCE_SHA256)
    print(f"source ready: {written}")


def _backfill(args: argparse.Namespace) -> None:
    csv_path = paths.source_dir() / fetch.SOURCE_FILE
    if not csv_path.exists():
        raise backfill.BackfillError(f"{csv_path} is missing; run `demo-world fetch` first")
    until: date = args.until
    summary = backfill.backfill(source.load_source(csv_path), paths.data_dir(), until)
    print(
        f"wrote {summary.bookings_files} bookings files and {summary.labels_files} labels files "
        f"before {until.isoformat()} to {paths.data_dir()}"
    )


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.run(args)
    except (fetch.FetchError, backfill.BackfillError) as error:
        print(f"demo-world: {error}", file=sys.stderr)
        return 1
    return 0
