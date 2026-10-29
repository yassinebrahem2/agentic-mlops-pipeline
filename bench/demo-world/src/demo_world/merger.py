"""The PR merger of evaluation runs: the world merges agents' PRs after a fixed simulated delay.

Each day it lists the open PRs of the run's copy. An agent's PR (head branch
`agent/<agent>/<task>`) is merged once `after_days` simulated days have passed since the world
first saw it open, if GitHub finds no conflict and its checks are green. A conflict, red checks
or a failed merge is recorded as not merged and left for the agents; it is tried again every
day, and recorded again only when the reason changes. A PR whose mergeability or checks are
still being computed waits, unrecorded. Without `--merge-after-days` the world never merges
(a live demo, where the user does).
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from demo_world import answer_key
from demo_world.github import GitHubError, Repository, call_json, failure
from demo_world.simulate import Step

# The stage 2 runtime's branch name for an agent's task: `agent/<agent>/<task>`.
AGENT_BRANCH = re.compile(r"agent/[^/]+/.+")
PULLS_FILE = "pull_requests.json"
_LIST_FIELDS = "number,headRefName,mergeable,statusCheckRollup"
# Check run conclusions and commit status states GitHub counts as passing.
_PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_WAITING_STATES = {"PENDING", "EXPECTED"}

type Verdict = Literal["merge", "wait"] | answer_key.Refusal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Pull:
    number: int
    branch: str
    mergeable: str
    rollup: list[dict[str, Any]]


def _checks(rollup: list[dict[str, Any]]) -> Literal["green", "pending", "red"]:
    """A PR's checks as one: red if one failed, pending if one runs or none reported yet."""
    pending = not rollup
    for check in rollup:
        if check.get("__typename") == "StatusContext":
            state = str(check.get("state"))
            if state in _WAITING_STATES:
                pending = True
            elif state != "SUCCESS":
                return "red"
        elif check.get("status") != "COMPLETED":
            pending = True
        elif check.get("conclusion") not in _PASSED:
            return "red"
    return "pending" if pending else "green"


def _verdict(pull: _Pull) -> Verdict:
    if pull.mergeable == "CONFLICTING":
        return "conflict"
    checks = _checks(pull.rollup)
    if checks == "red":
        return "checks_failed"
    if pull.mergeable != "MERGEABLE" or checks == "pending":
        return "wait"
    return "merge"


class Merger(Step):
    """Merges the copy's agent PRs `after_days` simulated days after they opened."""

    def __init__(self, repository: Repository, folder: Path, after_days: int) -> None:
        self.repository = repository
        self.folder = folder
        self.after_days = after_days

    def start_of_day(self, day: date, origin: date) -> None:
        # At the start, before the checkout's pull, so a merged PR takes effect the same day.
        try:
            pulls = self._open_pulls()
        except (GitHubError, KeyError, TypeError, ValueError) as error:
            log.error("no PR merged on %s: cannot list the open PRs: %s", day.isoformat(), error)
            return
        seen = self._seen()
        for pull in pulls:
            if AGENT_BRANCH.fullmatch(pull.branch):
                record = seen.setdefault(
                    str(pull.number), {"opened": day.isoformat(), "reason": None}
                )
                self._decide(pull, record, day)
        self._write_seen(seen)

    def _decide(self, pull: _Pull, record: dict[str, Any], day: date) -> None:
        opened = date.fromisoformat(record["opened"])
        delay = (day - opened).days
        if delay < self.after_days:
            return
        verdict = _verdict(pull)
        if verdict == "wait":
            return
        if verdict == "merge":
            args = ["pr", "merge", str(pull.number), "--repo", self.repository.name, "--merge"]
            result = self.repository.run(args)
            if result.ok:
                log.info("merged PR #%d (%s) on %s", pull.number, pull.branch, day.isoformat())
                self._append("pr_merged", pull, opened, day, None)
                record["reason"] = "merged"
                return
            log.error("PR #%d not merged: %s", pull.number, failure(args, result))
            verdict = "merge_failed"
        if record["reason"] != verdict:
            log.info("PR #%d not merged on %s: %s", pull.number, day.isoformat(), verdict)
            self._append("pr_not_merged", pull, opened, day, verdict)
            record["reason"] = verdict

    def _append(
        self,
        kind: Literal["pr_merged", "pr_not_merged"],
        pull: _Pull,
        opened: date,
        day: date,
        reason: answer_key.Refusal | None,
    ) -> None:
        decision = answer_key.PullDecision(
            kind,
            pull.number,
            pull.branch,
            opened.isoformat(),
            day.isoformat(),
            (day - opened).days,
            self.after_days,
            reason,
        )
        answer_key.append_new(self.folder, [decision])

    def _open_pulls(self) -> list[_Pull]:
        args = ["pr", "list", "--repo", self.repository.name, "--state", "open"]
        listed = call_json(self.repository.run, [*args, "--limit", "100", "--json", _LIST_FIELDS])
        return [
            _Pull(
                int(item["number"]),
                str(item["headRefName"]),
                str(item["mergeable"]),
                list(item["statusCheckRollup"] or []),
            )
            for item in listed
        ]

    def _seen(self) -> dict[str, dict[str, Any]]:
        # Each agent PR the world has seen: the business date it first saw it, its last reason.
        path = self.folder / PULLS_FILE
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_seen(self, seen: dict[str, dict[str, Any]]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / PULLS_FILE
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(seen, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
