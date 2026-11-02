from __future__ import annotations

import argparse

import pytest

from benchmarks.cli import COMMANDS, Command, main


def test_cli_help() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_cli_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    assert code == 0
    captured = capsys.readouterr()
    assert "bench" in captured.out


def test_cli_subcommand_dispatch() -> None:
    called: list[str] = []

    def run_dummy(args: argparse.Namespace) -> int:
        called.append(str(args.foo))
        return 42

    def configure_dummy(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--foo", required=True)

    dummy_cmd = Command(
        name="dummy",
        help="Dummy test command",
        register=configure_dummy,
        run=run_dummy,
    )
    COMMANDS.append(dummy_cmd)
    try:
        res = main(["dummy", "--foo", "bar"])
        assert res == 42
        assert called == ["bar"]
    finally:
        COMMANDS.remove(dummy_cmd)
