"""A fake `gh`: GitHub played in memory, recording every call; no test ever reaches GitHub.

It answers the calls the world makes (the user, the template, clones, Actions variables,
workflow dispatches and runs, pull requests) from its own state, which a test sets and reads.
"""

import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from demo_world.company import Finished
from tests.company_fixture import DAILY

NOT_FOUND = Finished(1, "", "gh: Not Found (HTTP 404)")


def checks(*conclusions: str) -> list[dict[str, str]]:
    """A status check rollup; `PENDING` is a check run still in progress."""
    return [
        {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}
        if c == "PENDING"
        else {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": c}
        for c in conclusions
    ]


@dataclass
class Pull:
    number: int
    branch: str
    mergeable: str = "MERGEABLE"
    rollup: list[dict[str, str]] = field(default_factory=lambda: checks("SUCCESS"))


@dataclass
class FakeGh:
    login: str = "tester"
    variables: dict[str, dict[str, str]] = field(default_factory=dict)
    pulls: list[Pull] = field(default_factory=list)
    merged: list[int] = field(default_factory=list)
    runs: list[int] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    # Calls (by their first words) that fail, as GitHub being down would make them.
    failing: list[tuple[str, ...]] = field(default_factory=list)
    # How many readiness checks a new copy fails before its template content lands.
    empty_checks: int = 0
    # Whether a dispatched run shows in the workflow's runs list.
    runs_listed: bool = True
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: Sequence[str]) -> Finished:
        argv = list(args)
        self.calls.append(argv)
        if any(tuple(argv[: len(f)]) == f for f in self.failing):
            return Finished(1, "", "gh: Server Error (HTTP 502)")
        if argv[0] == "repo":
            return self._clone(argv)
        if argv[0] == "pr":
            return self._pr(argv)
        method, endpoint = ("GET", argv[1]) if argv[1] != "-X" else (argv[2], argv[3])
        return self._api(method, endpoint, _fields(argv))

    def named(self, *words: str) -> list[list[str]]:
        """The calls that start with `words`."""
        return [c for c in self.calls if c[: len(words)] == list(words)]

    def _api(self, method: str, endpoint: str, fields: dict[str, str]) -> Finished:
        if endpoint == "user":
            return Finished(0, self.login + "\n", "")
        parts = endpoint.split("?")[0].split("/")
        if method == "POST" and parts[-1] == "generate":
            self.created.append(f"{fields['owner']}/{fields['name']}")
            return _json({"full_name": self.created[-1]})
        if parts[-1] == "commits":
            if self.empty_checks > 0:
                self.empty_checks -= 1
                return Finished(1, "", "gh: Git Repository is empty. (HTTP 409)")
            return _json([{"sha": "abc"}])
        if "variables" in parts:
            return self._variable(method, parts, fields)
        if method == "POST" and parts[-1] == "dispatches":
            self.runs.append(1000 + len(self.runs))
            return Finished(0, "", "")
        if parts[-1] == "runs":
            listed = self.runs if self.runs_listed else []
            return _json({"workflow_runs": [{"id": r} for r in reversed(listed)]})
        raise AssertionError(f"the fake gh does not know {method} {endpoint}")

    def _variable(self, method: str, parts: list[str], fields: dict[str, str]) -> Finished:
        name = parts[-1] if parts[-1] != "variables" else fields["name"]
        if method == "GET":
            return _json(self.variables[name]) if name in self.variables else NOT_FOUND
        if method == "POST":
            self.variables[name] = {"name": name, "value": fields["value"]}
        elif name not in self.variables:
            return NOT_FOUND
        elif method == "PATCH":
            self.variables[name] = {"name": name, "value": fields["value"]}
        else:
            del self.variables[name]
        return Finished(0, "", "")

    def _clone(self, argv: list[str]) -> Finished:
        destination = Path(argv[3])
        (destination / "ops").mkdir(parents=True)
        (destination / "ops" / "schedule.toml").write_text(DAILY, encoding="utf-8")
        return Finished(0, "", f"Cloning into '{destination}'...")

    def _pr(self, argv: list[str]) -> Finished:
        if argv[1] == "list":
            return _json(
                [
                    {
                        "number": p.number,
                        "headRefName": p.branch,
                        "mergeable": p.mergeable,
                        "statusCheckRollup": p.rollup,
                    }
                    for p in self.pulls
                ]
            )
        number = int(argv[2])
        self.merged.append(number)
        self.pulls = [p for p in self.pulls if p.number != number]
        return Finished(0, "", "")


def _fields(argv: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for flag, value in itertools.pairwise(argv):
        if flag in ("-f", "-F"):
            key, _, text = value.partition("=")
            fields[key] = text
    return fields


def _json(value: Any) -> Finished:
    return Finished(0, json.dumps(value), "")
