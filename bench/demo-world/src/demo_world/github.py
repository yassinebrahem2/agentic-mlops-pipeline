"""The one place the world calls GitHub: the `gh` CLI, behind a runner a test replaces.

Every GitHub call goes through a `GhRunner`; the command line takes the real one from `RUNNER`
at call time, so the tests swap that attribute and no test can reach GitHub. The world only
ever changes a run's own copy of the demo repository, never the original.
"""

import json
import os
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from demo_world import paths
from demo_world.company import Finished, run_process, tail

type GhRunner = Callable[[Sequence[str]], Finished]

_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_NOT_FOUND = "HTTP 404"


def run_gh(args: Sequence[str]) -> Finished:
    """Run `gh <args>` with the user's own `gh` login."""
    return run_process(["gh", *args], Path.cwd(), os.environ)


RUNNER: GhRunner = run_gh


class GitHubError(Exception):
    """A `gh` call failed, or the repository setting cannot be used."""


def checked_repository(name: str) -> str:
    """`name` as the company's repository, refused when it is malformed or the original."""
    if not _REPOSITORY.fullmatch(name):
        raise GitHubError(f"{paths.REPO_VARIABLE} must be owner/name, not {name!r}")
    if name.casefold() == paths.ORIGINAL_REPOSITORY.casefold():
        raise GitHubError(
            f"{paths.REPO_VARIABLE} names the original demo repository {name}, which no run "
            "may change; create a copy with `demo-world new-run <name>` and use `--run <name>`"
        )
    return name


def failure(args: Sequence[str], result: Finished) -> str:
    """A failed call as a log or error message: the command, its exit code, its output's end."""
    return (
        f"`gh {shlex.join(args)}` failed with exit code {result.code}:\n"
        f"{tail(result.stderr)}\n{tail(result.stdout)}"
    )


def not_found(result: Finished) -> bool:
    """Whether a failed `gh api` call was GitHub answering 404."""
    return not result.ok and _NOT_FOUND in result.stderr


def call(run: GhRunner, args: Sequence[str]) -> Finished:
    """Run `gh <args>`; a failure raises with the call and its output."""
    result = run(args)
    if not result.ok:
        raise GitHubError(failure(args, result))
    return result


def call_json(run: GhRunner, args: Sequence[str]) -> Any:
    """Run `gh <args>` and read its output as JSON."""
    result = call(run, args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GitHubError(f"`gh {shlex.join(args)}` printed no JSON: {error}") from error


@dataclass(frozen=True)
class Repository:
    """A run's copy of the demo repository, reached through `run`."""

    name: str
    run: GhRunner

    def __post_init__(self) -> None:
        checked_repository(self.name)

    def api(self, method: str, endpoint: str, *fields: str) -> list[str]:
        """The `gh api` arguments for `endpoint` under this repository, with `-f` fields."""
        args = ["api", "-X", method, f"repos/{self.name}/{endpoint}"]
        for field in fields:
            args += ["-f", field]
        return args
