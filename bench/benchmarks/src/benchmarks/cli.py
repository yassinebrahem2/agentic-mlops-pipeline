"""CLI for benchmarks."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    register: Callable[[argparse.ArgumentParser], None] | None = None
    run: Callable[[argparse.Namespace], int | None] | None = None


COMMANDS: list[Command] = []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench",
        description="Developer benchmarks for mlops-agents",
    )
    subparsers = parser.add_subparsers(dest="subcommand")
    for cmd in COMMANDS:
        sub = subparsers.add_parser(cmd.name, help=cmd.help)
        if cmd.register is not None:
            cmd.register(sub)
        if cmd.run is not None:
            sub.set_defaults(_handler=cmd.run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int | None] | None = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 0
    res = handler(args)
    return res if isinstance(res, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
