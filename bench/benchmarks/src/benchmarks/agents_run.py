"""Agents-run command: drives benchmark runs in agents mode against demo-world."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TextIO, cast

from demo_world import answer_key, newrun, paths, simulate
from demo_world.answer_key import Record

from benchmarks.client import BenchClient, sign_in
from benchmarks.jobs_days import day_starts_from_jobs
from benchmarks.report import Pause, RunInfo, write_report
from benchmarks.scorer import Score, score

log = logging.getLogger(__name__)

DEFAULT_END_DATE: Final[date] = date(2016, 9, 11)
DEFAULT_POLL_INTERVAL: Final[float] = 30.0
DEFAULT_MAINTENANCE_INTERVAL: Final[float] = 300.0  # 5 minutes
MAINTENANCE_WINDOW_DURATION: Final[timedelta] = timedelta(minutes=10)


@dataclass(frozen=True)
class WorldState:
    """The first simulated business day and the last day completed."""

    first: date
    last_done: date


def read_world_state(world_dir: Path) -> WorldState | None:
    """Reads state.json from world_dir, returning None if missing or incomplete."""
    path = world_dir / simulate.STATE_FILE
    if not path.is_file():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    data_dict = cast(dict[str, object], data)
    first_raw = data_dict.get("first")
    last_raw = data_dict.get("last_done")
    if not isinstance(first_raw, str) or not isinstance(last_raw, str):
        return None
    try:
        return WorldState(date.fromisoformat(first_raw), date.fromisoformat(last_raw))
    except ValueError:
        return None


def _read_env_settings(path: Path) -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, equals, value = line.partition("=")
        if equals:
            settings[name.strip()] = value.strip()
    return settings


def resolve_run_paths(run: str | Path) -> tuple[Path, Path, Path]:
    """Resolves (run_dir, world_dir, data_dir) from a run name or path."""
    p = Path(run)
    if p.exists():
        if (p / newrun.RUN_FILE).is_file():
            try:
                settings = _read_env_settings(p / newrun.RUN_FILE)
                world_dir = Path(settings.get(paths.WORLD_VARIABLE, str(p / "world")))
                data_dir = Path(settings.get(paths.DATA_VARIABLE, str(p / "data")))
                return p, world_dir, data_dir
            except OSError:
                pass
        if (p / simulate.STATE_FILE).is_file() or (p / simulate.HOLD_FILE).is_file():
            # p is world_dir
            return p.parent, p, p.parent / "data"
        if (p / "world").is_dir():
            return p, p / "world", p / "data"
        return p, p, p

    try:
        folder = newrun.run_folder(str(run))
        if (folder / newrun.RUN_FILE).is_file():
            settings = newrun.load_run(str(run))
            world_dir = Path(settings[paths.WORLD_VARIABLE])
            data_dir = Path(settings[paths.DATA_VARIABLE])
            return folder, world_dir, data_dir
        return folder, folder / "world", folder / "data"
    except newrun.RunError:
        return p, p / "world", p / "data"


def _get_app_commit() -> str:
    repo_root = Path(__file__).resolve().parents[4]
    git_bin = shutil.which("git") or "git"
    res = subprocess.run(  # noqa: S603
        [git_bin, "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    commit = res.stdout.strip()[:8]
    if not commit:
        raise RuntimeError("git rev-parse HEAD returned empty commit")
    return commit


def _extract_field(item: object, field: str) -> object:
    if isinstance(item, dict):
        return cast(dict[str, object], item).get(field)
    return getattr(item, field, None)


def _extract_string_field(item: object, field: str) -> str | None:
    val = _extract_field(item, field)
    return str(val) if val is not None else None


def _extract_expectation_refs(raw_exp: object) -> list[str]:
    if not isinstance(raw_exp, list):
        return []
    items = cast(list[object], raw_exp)
    scope: list[str] = []
    for item in items:
        key_obj = _extract_field(item, "key")
        ref_obj = _extract_field(key_obj, "ref") if key_obj is not None else None
        if isinstance(ref_obj, str) and ref_obj and ref_obj not in scope:
            scope.append(ref_obj)
    return scope


def _is_session_active(sess: object) -> bool:
    busy_obj = _extract_field(sess, "busy")
    busy = bool(busy_obj) if busy_obj is not None else False
    msgs_obj = _extract_field(sess, "messages")
    msgs = cast(list[object], msgs_obj) if isinstance(msgs_obj, list) else []
    active_msg = any(_extract_string_field(m, "status") in ("running", "queued") for m in msgs)
    return busy or active_msg


def _filter_stall_approvals(approvals: object) -> list[object]:
    if not isinstance(approvals, list):
        return []
    result: list[object] = []
    for item in cast(list[object], approvals):
        kind = _extract_string_field(item, "kind")
        if kind == "stall":
            result.append(item)
    return result


def _find_open_pr_number(gh_items: object) -> int | None:
    if not isinstance(gh_items, list):
        return None
    for item in cast(list[object], gh_items):
        kind = _extract_string_field(item, "kind")
        state = _extract_string_field(item, "state")
        if kind == "pr" and state == "open":
            num_val = _extract_field(item, "number")
            if isinstance(num_val, int):
                return num_val
    return None


def _extract_stall_event_ids(activity_page: object) -> list[str]:
    entries_obj = _extract_field(activity_page, "entries")
    entries = cast(list[object], entries_obj) if isinstance(entries_obj, list) else []
    stall_ids: list[str] = []
    for entry in entries:
        ev = _extract_field(entry, "event")
        if ev is None:
            continue
        ev_type = _extract_string_field(ev, "type")
        ev_id = _extract_string_field(ev, "id")
        if ev_type == "turn.stalled" and ev_id:
            stall_ids.append(ev_id)
    return stall_ids


def _extract_model_and_provider(agents_list: object) -> tuple[str | None, str | None]:
    if not isinstance(agents_list, list):
        return None, None
    for ag in cast(list[object], agents_list):
        settings = _extract_field(ag, "settings")
        if settings is None:
            continue
        eff = _extract_field(settings, "effective_model")
        if isinstance(eff, str) and eff:
            if "/" in eff:
                prov, _, mod = eff.partition("/")
                return prov, mod
            return None, eff
    return None, None


async def maintain_hold_windows(
    client: BenchClient,
    project_id: str,
    now: datetime,
) -> None:
    """Keeps a maintenance window open across all expectation refs during a hold."""
    raw_exp: object = await client.get(f"/projects/{project_id}/expectations")
    scope = _extract_expectation_refs(raw_exp)
    if not scope:
        return

    body: dict[str, Any] = {
        "start": now.isoformat(),
        "end": (now + MAINTENANCE_WINDOW_DURATION).isoformat(),
        "scope": scope,
        "reason": "benchmark hold",
    }
    await client.post(f"/projects/{project_id}/maintenance-windows", body=body)


async def check_readiness(
    client: BenchClient,
    project_id: str,
) -> tuple[bool, str | None]:
    """Checks whether the project is ready to begin day 1.

    Returns (is_ready, waiting_reason). When not ready, waiting_reason states
    what is being waited for, nothing else.
    """
    # 1. Handoff exists
    handoff: object = await client.get(f"/projects/{project_id}/handoff")
    if handoff is None:
        return False, "waiting for handoff"

    # 2. No agent's session has a running or queued message
    agents_list: object = await client.get(f"/projects/{project_id}/agents")
    if isinstance(agents_list, list):
        for ag in cast(list[object], agents_list):
            chat_val = _extract_field(ag, "chat")
            if chat_val is False:
                continue
            ag_name = _extract_string_field(ag, "name")
            if not ag_name:
                continue
            with contextlib.suppress(Exception):
                sess: object = await client.get(f"/projects/{project_id}/agents/{ag_name}/session")
                if _is_session_active(sess):
                    return False, f"waiting for agent {ag_name} session to finish"

    # 3. No approval is pending
    approvals: object = await client.get(f"/projects/{project_id}/approvals")
    if isinstance(approvals, list) and len(cast(list[object], approvals)) > 0:
        return False, f"waiting for {len(cast(list[object], approvals))} pending approval(s)"

    # 4. No agent PR is open
    gh_items: object = await client.get(f"/projects/{project_id}/github/activity")
    open_pr_num = _find_open_pr_number(gh_items)
    if open_pr_num is not None:
        return False, f"waiting for open PR #{open_pr_num}"

    return True, None


async def drive_agents_run(
    client: BenchClient,
    project_id: str,
    run: str | Path,
    *,
    end_date: date = DEFAULT_END_DATE,
    scenario_name: str = "agents-short",
    seconds_per_day: float | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    maintenance_interval: float = DEFAULT_MAINTENANCE_INTERVAL,
    run_dir: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stdout: TextIO = sys.stdout,
) -> Score:
    """Drives an agents benchmark run in order:

    1. Wait before day 1 until ready, holding world and maintaining windows.
    2. Play business days, creating hold on model stall and resuming when stall approvals clear.
    3. Maintain maintenance windows every 5 minutes during every hold.
    4. Score and report after the last simulated day completes.
    """
    resolved_run_dir, world_dir, data_dir = resolve_run_paths(run)
    final_run_dir = run_dir if run_dir is not None else resolved_run_dir
    hold_file = world_dir / simulate.HOLD_FILE

    run_started = clock()
    pauses: list[Pause] = []

    # --- Phase 1: Wait before day 1 ---
    wait_start = clock()
    last_maint_time: datetime | None = None
    last_printed_reason: str | None = None

    while True:
        now = clock()
        if (
            last_maint_time is None
            or (now - last_maint_time).total_seconds() >= maintenance_interval
        ):
            await maintain_hold_windows(client, project_id, now)
            last_maint_time = now

        is_ready, waiting_reason = await check_readiness(client, project_id)
        if is_ready:
            break

        if waiting_reason != last_printed_reason and waiting_reason is not None:
            stdout.write(f"{waiting_reason}\n")
            stdout.flush()
            last_printed_reason = waiting_reason

        await sleep(poll_interval)

    wait_end = clock()
    pauses.append(Pause(reason="ready_wait", start=wait_start, end=wait_end))
    if hold_file.exists():
        with contextlib.suppress(OSError):
            hold_file.unlink()

    # --- Phase 2: Play, holding on a stall ---
    seen_stall_event_ids: set[str] = set()

    while True:
        state = read_world_state(world_dir)
        if state is not None and state.last_done >= end_date:
            break

        stall_detected = False
        activity_page: object = await client.get(
            f"/projects/{project_id}/activity", params={"type": "turn.stalled"}
        )
        new_stall_ids = [
            sid
            for sid in _extract_stall_event_ids(activity_page)
            if sid not in seen_stall_event_ids
        ]
        if new_stall_ids:
            seen_stall_event_ids.update(new_stall_ids)
            stall_detected = True

        approvals: object = await client.get(f"/projects/{project_id}/approvals")
        if _filter_stall_approvals(approvals):
            stall_detected = True

        if stall_detected:
            stall_start = clock()
            with contextlib.suppress(OSError):
                hold_file.touch(exist_ok=True)

            last_stall_maint: datetime | None = None
            while True:
                now = clock()
                if (
                    last_stall_maint is None
                    or (now - last_stall_maint).total_seconds() >= maintenance_interval
                ):
                    await maintain_hold_windows(client, project_id, now)
                    last_stall_maint = now

                cur_approvals: object = await client.get(f"/projects/{project_id}/approvals")
                if not _filter_stall_approvals(cur_approvals):
                    stall_end = clock()
                    pauses.append(Pause(reason="model_stall", start=stall_start, end=stall_end))
                    if hold_file.exists():
                        with contextlib.suppress(OSError):
                            hold_file.unlink()
                    break

                await sleep(poll_interval)

        await sleep(poll_interval)

    # --- Phase 4: Score and report ---
    state = read_world_state(world_dir)
    if state is None:
        raise RuntimeError(
            f"World state file {world_dir / simulate.STATE_FILE} not found or incomplete"
        )

    jobs_dir = data_dir / "jobs"
    starts, gaps = day_starts_from_jobs(jobs_dir, date_range=(state.first, state.last_done))

    records: list[Record] = []
    key_path = world_dir / answer_key.ANSWER_KEY_FILE
    if key_path.is_file():
        for line in key_path.read_text(encoding="utf-8").splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            try:
                d: object = json.loads(line_str)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(d, dict)
                and "kind" in d
                and "category" in d
                and "start" in d
                and "end" in d
            ):
                raw_d = cast(dict[str, object], d)
                col_val = raw_d.get("column")
                col_str = str(col_val) if col_val is not None else None
                records.append(
                    Record(
                        kind=str(raw_d["kind"]),
                        category=cast(answer_key.Category, raw_d["category"]),
                        column=col_str,
                        start=str(raw_d["start"]),
                        end=str(raw_d["end"]),
                    )
                )

    score_res = await score(client, project_id, records, starts=starts)

    agents_list: object = await client.get(f"/projects/{project_id}/agents")
    provider, model = _extract_model_and_provider(agents_list)

    if provider is None:
        try:
            provs: object = await client.get("/providers")
            if isinstance(provs, list) and len(cast(list[object], provs)) > 0:
                p0 = cast(list[object], provs)[0]
                p_name = _extract_string_field(p0, "name")
                if p_name:
                    provider = p_name
        except Exception:
            log.warning("Could not read providers from /providers")

    app_commit = _get_app_commit()
    run_ended = clock()

    info = RunInfo(
        mode="agents",
        dataset="hotel-demo",
        scenario=scenario_name,
        seed=None,
        app_commit=app_commit,
        started=run_started,
        ended=run_ended,
        provider=provider,
        model=model,
        seconds_per_day=seconds_per_day,
        pauses=pauses,
        day_gaps=gaps,
    )

    await write_report(client, project_id, final_run_dir, info, score_res)
    return score_res


async def agents_run_cli(
    *,
    run: str | Path,
    project_id: str,
    base_url: str,
    end_date: date = DEFAULT_END_DATE,
    scenario_name: str = "agents-short",
    seconds_per_day: float | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    run_dir: Path | None = None,
) -> Score:
    client = await sign_in(base_url)
    return await drive_agents_run(
        client=client,
        project_id=project_id,
        run=run,
        end_date=end_date,
        scenario_name=scenario_name,
        seconds_per_day=seconds_per_day,
        poll_interval=poll_interval,
        run_dir=run_dir,
    )


def register_agents_run_cli(parser: argparse.ArgumentParser) -> None:
    """Registers arguments for the `bench agents-run` subcommand."""
    parser.add_argument(
        "--run",
        required=True,
        help="demo-world run name or folder",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project ID in the app",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="Base URL of the app (e.g. http://127.0.0.1:8765)",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=DEFAULT_END_DATE,
        help="Last business date of the run (default: 2016-09-11)",
    )
    parser.add_argument(
        "--scenario",
        default="agents-short",
        help="Scenario name or TOML path (default: agents-short)",
    )
    parser.add_argument(
        "--seconds-per-day",
        type=float,
        default=None,
        help="Pace in seconds per simulated day",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="Poll interval in seconds (default: 30.0)",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Directory to write report (defaults to run's folder)",
    )


def run_agents_run_cli(args: argparse.Namespace) -> int:
    """CLI handler for `bench agents-run`."""
    try:
        score_res = asyncio.run(
            agents_run_cli(
                run=str(args.run),
                project_id=str(args.project),
                base_url=str(args.base_url),
                end_date=cast(date, args.end),
                scenario_name=str(args.scenario),
                seconds_per_day=cast(float | None, args.seconds_per_day),
                poll_interval=float(args.poll_interval),
                run_dir=cast(Path | None, args.run_dir),
            )
        )
        sys.stdout.write(
            f"Agents run complete: {len(score_res.matched)} matched, {len(score_res.misses)} misses\n"
        )
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1


__all__ = [
    "DEFAULT_END_DATE",
    "DEFAULT_MAINTENANCE_INTERVAL",
    "DEFAULT_POLL_INTERVAL",
    "WorldState",
    "agents_run_cli",
    "check_readiness",
    "drive_agents_run",
    "maintain_hold_windows",
    "read_world_state",
    "register_agents_run_cli",
    "resolve_run_paths",
    "run_agents_run_cli",
]
