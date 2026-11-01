"""A fake company checkout for the tests: the fake CLI script, a schedule, a recording runner."""

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from demo_world import company, paths

FAKE_CLI = Path(__file__).parent / "fake_company.py"
DAILY = '[[jobs]]\nname = "daily"\ncommand = "hotel-demo daily --date {date}"\nevery = 1\n'


def job(name: str, command: str, every: int) -> str:
    return f'\n[[jobs]]\nname = "{name}"\ncommand = "{command}"\nevery = {every}\n'


def make_checkout(root: Path, schedule: str = DAILY) -> Path:
    checkout = root / "company"
    (checkout / "ops").mkdir(parents=True)
    (checkout / "ops" / "schedule.toml").write_text(schedule, encoding="utf-8")
    return checkout


def add_to_schedule(checkout: Path, text: str) -> None:
    with (checkout / "ops" / "schedule.toml").open("a", encoding="utf-8") as file:
        file.write(text)


def fail(checkout: Path, *commands: str) -> None:
    """Make the fake CLI fail every call that starts with one of `commands`."""
    (checkout / "failures.json").write_text(json.dumps(commands), encoding="utf-8")


def calls(checkout: Path) -> list[dict[str, Any]]:
    path = checkout / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def commands(checkout: Path) -> list[str]:
    """Each call the fake CLI received, as one line; the probe's temporary folder left out."""
    return [
        " ".join(c["argv"][:3]) if c["argv"][0] == "mlflow" else " ".join(c["argv"])
        for c in calls(checkout)
    ]


class Recorder:
    """A runner that records every command, answers `git` itself and runs everything else."""

    def __init__(self, on_git: Callable[[], company.Finished] | None = None) -> None:
        self.argvs: list[list[str]] = []
        self.cwds: list[Path] = []
        self._on_git = on_git

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> company.Finished:
        self.argvs.append(list(argv))
        self.cwds.append(cwd)
        if argv[0] == "git":
            return (
                self._on_git() if self._on_git else company.Finished(0, "Already up to date.", "")
            )
        return company.run_process(argv, cwd, env)


def fake_company(checkout: Path, runner: company.Runner | None = None) -> company.Company:
    """The company at `checkout`, its CLI played by the fake script, with the world's folders."""
    return company.Company(
        checkout=checkout,
        data=paths.data_dir(),
        mlflow=paths.mlflow_dir(),
        launcher=(sys.executable, str(FAKE_CLI)),
        runner=Recorder() if runner is None else runner,
    )
