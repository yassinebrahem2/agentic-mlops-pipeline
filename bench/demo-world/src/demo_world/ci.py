"""The company's CI at simulation speed: its scheduled workflow, dispatched every N simulated days.

So the copy's Actions history moves with simulated time. Each dispatch is recorded in the
world's folder (business date, real time, run ID), never in the answer key or the data folder,
so a CI incident can be placed in business days even at full speed.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from demo_world.github import GitHubError, Repository, call_json, failure
from demo_world.simulate import Step

# The demo repository's CI smoke run: `on: schedule` and `workflow_dispatch`, no inputs.
SCHEDULED_WORKFLOW = "scheduled.yml"
DEFAULT_BRANCH = "main"
DISPATCHES_FILE = "ci_dispatches.jsonl"
DEFAULT_EVERY = 7
# A dispatched run takes a few seconds to show in the workflow's runs.
RUN_ATTEMPTS = 5
RUN_WAIT_SECONDS = 2.0
_WORKFLOW = f"actions/workflows/{SCHEDULED_WORKFLOW}"

type Clock = Callable[[], datetime]

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def real_time(moment: datetime) -> str:
    """A real (wall clock) time as the world records it: ISO 8601 with its UTC offset."""
    return moment.isoformat(timespec="seconds")


# Subclassing the Protocol gives the steps it does not need its no-op methods.
@dataclass
class Dispatcher(Step):
    """Dispatches the scheduled workflow on every `every`-th day counted from the first day."""

    repository: Repository
    folder: Path
    every: int
    now: Clock = utc_now
    sleep: Callable[[float], None] = time.sleep

    def end_of_day(self, day: date, origin: date) -> None:
        # A replayed day (a stop before it was done) is not dispatched a second time.
        if (day - origin).days % self.every != 0 or day.isoformat() in self._dispatched():
            return
        before = self._run_ids()
        dispatched_at = self.now()
        args = self.repository.api("POST", f"{_WORKFLOW}/dispatches", f"ref={DEFAULT_BRANCH}")
        result = self.repository.run(args)
        if not result.ok:
            log.error("CI not dispatched on %s: %s", day.isoformat(), failure(args, result))
            return
        run_id = None if before is None else self._new_run(before)
        if run_id is None:
            log.warning(
                "no run found for the CI dispatch of %s; recorded without a run ID",
                day.isoformat(),
            )
        record = {
            "business_date": day.isoformat(),
            "dispatched_at": real_time(dispatched_at),
            "run_id": run_id,
        }
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / DISPATCHES_FILE).open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(record) + "\n")

    def _dispatched(self) -> set[str]:
        path = self.folder / DISPATCHES_FILE
        if not path.exists():
            return set()
        lines = path.read_text(encoding="utf-8").splitlines()
        return {json.loads(line)["business_date"] for line in lines}

    def _run_ids(self) -> set[int] | None:
        args = self.repository.api("GET", f"{_WORKFLOW}/runs?event=workflow_dispatch&per_page=20")
        try:
            listed = call_json(self.repository.run, args)
            return {int(run["id"]) for run in listed["workflow_runs"]}
        except (GitHubError, KeyError, TypeError, ValueError) as error:
            log.error("cannot list the CI runs: %s", error)
            return None

    def _new_run(self, before: set[int]) -> int | None:
        # The dispatch API answers without the run; the new run is the one not listed before.
        for attempt in range(RUN_ATTEMPTS):
            ids = self._run_ids()
            if ids is None:
                return None
            if new := ids - before:
                return max(new)
            if attempt < RUN_ATTEMPTS - 1:
                self.sleep(RUN_WAIT_SECONDS)
        return None
