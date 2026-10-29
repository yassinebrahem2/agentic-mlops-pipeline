"""The one place the world runs the company: its CLI, its schedule, its checkout.

The world shares no code with the company. It runs the company's commands in the company's own
environment (`uv run --project <checkout>`), as the company's ops host would, and knows only what
the company documents: the `hotel-demo` CLI, the `ops/schedule.toml` format, and where its MLflow
store is. A failing command is logged with its output and never raised, so a broken company day
never stops the world's clock.
"""

import logging
import os
import re
import shlex
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from demo_world import paths

CLI = "hotel-demo"
SCHEDULE_FILE = Path("ops") / "schedule.toml"
# The company's README: the model is registered as `hotel-cancellation`, served by its
# `production` alias.
PRODUCTION_MODEL_URI = "models:/hotel-cancellation@production"
# MLflow's two answers when the alias resolves to nothing: no such model, or no such alias.
_NOT_FOUND = re.compile(r"Registered model .*not found", re.IGNORECASE)
_TAIL_LINES = 20

log = logging.getLogger(__name__)


class CompanyError(Exception):
    """The company checkout or its schedule cannot be used."""


@dataclass(frozen=True)
class Finished:
    """A finished process: its exit code and what it printed."""

    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0


type Runner = Callable[[Sequence[str], Path, Mapping[str, str]], Finished]


def run_process(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Finished:
    """Run a process to the end; a program that cannot start is a failure like any other."""
    try:
        done = subprocess.run(  # noqa: S603 (the world's own commands, never a shell)
            list(argv),
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        return Finished(127, "", f"cannot start {argv[0]}: {error}")
    return Finished(done.returncode, done.stdout, done.stderr)


def uv_launcher(checkout: Path) -> tuple[str, ...]:
    """The prefix that runs a program of the company's environment."""
    return ("uv", "run", "--project", str(checkout))


@dataclass(frozen=True)
class Job:
    """One entry of the company's schedule: a CLI command run every `every` business days."""

    name: str
    command: str
    every: int

    def due(self, day: date, origin: date) -> bool:
        return (day - origin).days % self.every == 0


def _job(number: int, table: Mapping[str, Any]) -> Job:
    name, command, every = table.get("name"), table.get("command"), table.get("every")
    if not isinstance(name, str) or not isinstance(command, str):
        raise CompanyError(f"job {number}: 'name' and 'command' must be text")
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        raise CompanyError(
            f"job {number} ({name}): 'every' must be a whole number of days, 1 or more"
        )
    return Job(name, command, every)


def read_schedule(checkout: Path) -> tuple[Job, ...]:
    """The jobs of the checkout's `ops/schedule.toml`, in file order."""
    path = checkout / SCHEDULE_FILE
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except OSError as error:
        raise CompanyError(f"cannot read the schedule {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise CompanyError(f"the schedule {path} is not valid TOML: {error}") from error
    tables = document.get("jobs", [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise CompanyError(f"the schedule {path}: 'jobs' must be an array of tables ([[jobs]])")
    return tuple(_job(number, table) for number, table in enumerate(tables, start=1))


class Production(Enum):
    """Whether the company has a production model; UNKNOWN when MLflow could not say."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def tail(text: str) -> str:
    """The last lines of a command's output, for a log line."""
    return "\n".join(text.strip().splitlines()[-_TAIL_LINES:])


@dataclass(frozen=True)
class Company:
    """The company at a checkout, run against the world's data and MLflow folders."""

    checkout: Path
    data: Path
    mlflow: Path
    launcher: tuple[str, ...]
    runner: Runner = run_process

    @classmethod
    def at(
        cls, checkout: Path, data: Path, mlflow: Path, runner: Runner = run_process
    ) -> "Company":
        return cls(checkout, data, mlflow, uv_launcher(checkout), runner)

    def store(self) -> Path:
        """The company's documented default MLflow store, the one its server serves."""
        return (self.mlflow / "mlflow.db").resolve()

    def environment(self, tracking_uri: str | None = None) -> dict[str, str]:
        """The environment the company's programs run with: the world's folders set."""
        env = dict(os.environ)
        # The world's own uv environment; uv would only warn that it ignores it.
        env.pop("VIRTUAL_ENV", None)
        env[paths.DATA_VARIABLE] = str(self.data)
        env[paths.MLFLOW_VARIABLE] = str(self.mlflow)
        if tracking_uri is not None:
            env[paths.TRACKING_URI_VARIABLE] = tracking_uri
        return env

    def _run(
        self, command: Sequence[str], env: Mapping[str, str], *, launched: bool, quiet: bool = False
    ) -> Finished:
        """Run `command`, from the company's environment when `launched`, in the checkout."""
        argv = [*self.launcher, *command] if launched else list(command)
        result = self.runner(argv, self.checkout, env)
        shown = shlex.join(command)
        if not result.ok and not quiet:
            log.error(
                "`%s` failed with exit code %d:\n%s\n%s",
                shown,
                result.code,
                tail(result.stderr),
                tail(result.stdout),
            )
        elif result.ok:
            log.info("`%s` done", shown)
        return result

    def update(self) -> Finished:
        """Bring the checkout to the latest `main`, so merged PRs take effect."""
        return self._run(["git", "pull", "--ff-only"], self.environment(), launched=False)

    def cli(self, args: Sequence[str]) -> Finished:
        """Run `hotel-demo <args>` in the company's environment."""
        return self._run([CLI, *args], self.environment(), launched=True)

    def production_model(self) -> Production:
        """Ask MLflow, from the company's environment, whether `production` names a model."""
        # Empty counts as unset, as in the company's `paths.tracking_uri()`.
        tracking_uri = os.environ.get(paths.TRACKING_URI_VARIABLE) or None
        if tracking_uri is None:
            store = self.store()
            if not store.exists():
                return Production.ABSENT
            # The company's documented default store; set for this probe only, since the
            # MLflow CLI does not read HOTEL_DEMO_MLFLOW.
            tracking_uri = f"sqlite:///{store.as_posix()}"
        with tempfile.TemporaryDirectory(prefix="demo-world-probe-") as folder:
            command = ["mlflow", "artifacts", "download", "--artifact-uri", PRODUCTION_MODEL_URI]
            command += ["--dst-path", folder]
            env = self.environment(tracking_uri)
            result = self._run(command, env, launched=True, quiet=True)
        if result.ok:
            return Production.PRESENT
        if _NOT_FOUND.search(result.stderr):
            return Production.ABSENT
        log.error(
            "cannot tell whether the company has a production model (exit code %d):\n%s",
            result.code,
            tail(result.stderr),
        )
        return Production.UNKNOWN

    def bootstrap(self, cutoff: date) -> None:
        """Train, evaluate and deploy a first model, unless the company already has one."""
        # On UNKNOWN the probe has logged why; training then might replace a working model.
        if self.production_model() is not Production.ABSENT:
            return
        trained = self.cli(["train", "--cutoff", cutoff.isoformat()])
        if not trained.ok:
            return
        lines = trained.stdout.strip().splitlines()
        version = lines[-1].strip() if lines else ""
        if not version.isdigit():
            log.error("`%s train` did not print a model version last: %r", CLI, version)
            return
        if self.cli(["evaluate", "--version", version]).ok:
            self.cli(["deploy", "--version", version])

    def run_jobs(self, day: date, origin: date) -> None:
        """Run every job of the schedule due on `day`, in file order, counted from `origin`."""
        try:
            jobs = read_schedule(self.checkout)
        except CompanyError as error:
            log.error("no company jobs run on %s: %s", day.isoformat(), error)
            return
        for job in jobs:
            if not job.due(day, origin):
                continue
            try:
                argv = shlex.split(job.command.replace("{date}", day.isoformat()))
            except ValueError as error:
                log.error("job %r skipped: its command cannot be split: %s", job.name, error)
                continue
            if argv[:1] != [CLI]:
                log.error("job %r skipped: its command does not start with %s", job.name, CLI)
                continue
            self.cli(argv[1:])
