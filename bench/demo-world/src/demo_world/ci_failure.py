"""The `ci_failure` injection: the company CI's sample data goes missing for a window of days.

At the window's first day the world points the copy's `SAMPLE_DATA_URL` Actions variable at a
file that does not exist, so the scheduled workflow's download fails; on the first day after
the window, and on every exit of the loop, it restores the variable as it was (set to its
value, or deleted when it was unset, which the workflow reads as its default URL).

The original value is written to the world's folder before the variable changes, so a run
killed inside the window (no exit code ran) is repaired by the next run from that record, and
a resumed day inside the window never mistakes the missing file for the original.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from demo_world import answer_key
from demo_world.ci import Clock, real_time, utc_now
from demo_world.github import GitHubError, Repository, failure, not_found
from demo_world.scenarios import Problem
from demo_world.simulate import Step

# The variable the company's scheduled workflow downloads its sample data from.
SAMPLE_DATA_VARIABLE = "SAMPLE_DATA_URL"
MARKER_FILE = "ci_failure.json"
_VARIABLES = "actions/variables"

log = logging.getLogger(__name__)


def missing_sample_data(repository: str) -> str:
    """A URL in the copy itself that answers 404, as a deleted upstream file would."""
    return f"https://raw.githubusercontent.com/{repository}/main/ops/missing-sample-data.tar.gz"


@dataclass(frozen=True)
class _Broken:
    """The record of a variable the world changed: what to restore, and when it changed."""

    original: str | None
    start: str
    end: str
    set_on: str
    set_at: str


def _same_window(broken: _Broken, window: Problem) -> bool:
    return (broken.start, broken.end) == (window.start.isoformat(), window.end.isoformat())


class CiFailure(Step):
    """Breaks the CI's sample data during each `ci_failure` window and restores it after."""

    def __init__(
        self,
        repository: Repository,
        folder: Path,
        problems: Sequence[Problem],
        now: Clock = utc_now,
    ) -> None:
        self.repository = repository
        self.folder = folder
        self.windows = tuple(p for p in problems if p.kind == "ci_failure")
        self.now = now
        self._today: date | None = None

    def start_of_day(self, day: date, origin: date) -> None:
        # At the start, before the day's CI dispatch, so a dispatch in the window sees it.
        self._today = day
        window = next((w for w in self.windows if w.active(day)), None)
        broken = self._broken()
        if broken is not None and (window is None or not _same_window(broken, window)):
            self._restore(broken, day)
            broken = self._broken()
        if broken is None and window is not None:
            self._break(window, day)

    def close(self) -> None:
        broken = self._broken()
        if broken is not None:
            self._restore(broken, self._today or date.fromisoformat(broken.set_on))

    def _marker(self) -> Path:
        return self.folder / MARKER_FILE

    def _broken(self) -> _Broken | None:
        if not self._marker().exists():
            return None
        return _Broken(**json.loads(self._marker().read_text(encoding="utf-8")))

    def _variable_args(self, method: str, value: str | None = None) -> list[str]:
        endpoint = _VARIABLES if method == "POST" else f"{_VARIABLES}/{SAMPLE_DATA_VARIABLE}"
        if value is None:
            return self.repository.api(method, endpoint)
        return self.repository.api(
            method, endpoint, f"name={SAMPLE_DATA_VARIABLE}", f"value={value}"
        )

    def _current(self) -> str | None:
        """The variable's value, None when unset; raises when GitHub cannot say."""
        args = self._variable_args("GET")
        result = self.repository.run(args)
        if not_found(result):
            return None
        if not result.ok:
            raise GitHubError(failure(args, result))
        try:
            return str(json.loads(result.stdout)["value"])
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise GitHubError(f"{SAMPLE_DATA_VARIABLE} read without a value: {error}") from error

    def _set(self, value: str, exists: bool) -> bool:
        args = self._variable_args("PATCH" if exists else "POST", value)
        result = self.repository.run(args)
        if not_found(result) and exists:
            # Deleted since it was read: create it instead.
            return self._set(value, exists=False)
        if not result.ok:
            log.error("cannot set %s: %s", SAMPLE_DATA_VARIABLE, failure(args, result))
        return result.ok

    def _break(self, window: Problem, day: date) -> None:
        try:
            original = self._current()
        except GitHubError as error:
            log.error("ci_failure not started on %s: %s", day.isoformat(), error)
            return
        start, end = window.start.isoformat(), window.end.isoformat()
        set_at = real_time(self.now())
        broken = _Broken(original, start, end, day.isoformat(), set_at)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._marker().write_text(json.dumps(asdict(broken)) + "\n", encoding="utf-8")
        if not self._set(missing_sample_data(self.repository.name), original is not None):
            self._marker().unlink()
            return
        event = answer_key.CiFailureEvent(
            "ci_failure_set", start, end, day.isoformat(), set_at, set_at
        )
        answer_key.append_new(self.folder, [event])
        log.info("ci_failure: %s points at a missing file from %s", SAMPLE_DATA_VARIABLE, day)

    def _restore(self, broken: _Broken, day: date) -> None:
        if broken.original is None:
            args = self._variable_args("DELETE")
            result = self.repository.run(args)
            restored = result.ok or not_found(result)
            if not restored:
                log.error("cannot delete %s: %s", SAMPLE_DATA_VARIABLE, failure(args, result))
        else:
            restored = self._set(broken.original, exists=True)
        if not restored:
            log.error(
                "%s still points at a missing file; tried again on the next day and on exit",
                SAMPLE_DATA_VARIABLE,
            )
            return
        already = any(
            line["kind"] == "ci_failure_restored" and line["set_at"] == broken.set_at
            for line in answer_key.lines(self.folder)
        )
        if not already:
            event = answer_key.CiFailureEvent(
                "ci_failure_restored",
                broken.start,
                broken.end,
                day.isoformat(),
                real_time(self.now()),
                broken.set_at,
            )
            answer_key.append_new(self.folder, [event])
        self._marker().unlink()
        log.info("ci_failure: %s restored on %s", SAMPLE_DATA_VARIABLE, day)
