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

import httpx2
from demo_world import answer_key, newrun, paths, simulate
from demo_world.answer_key import Record

from benchmarks.client import BenchClient, BenchConnectionError, BenchHttpError, sign_in
from benchmarks.jobs_days import day_starts_from_jobs
from benchmarks.report import Pause, RunInfo, write_report
from benchmarks.scorer import AlreadyScored, Score, score

log = logging.getLogger(__name__)

ROUTE_ERRORS = (BenchHttpError, BenchConnectionError, httpx2.HTTPError)


def is_route_error(exc: BaseException) -> bool:
    """Returns True if the exception represents an HTTP/route/connection error."""
    if isinstance(exc, ROUTE_ERRORS):
        return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return (
        "route" in name
        or "route error" in msg
        or "connection error" in msg
        or "500 internal" in msg
        or "http" in name
    )


DEFAULT_END_DATE: Final[date] = date(2016, 9, 11)
DEFAULT_POLL_INTERVAL: Final[float] = 30.0
DEFAULT_MAINTENANCE_INTERVAL: Final[float] = 300.0  # 5 minutes
DEFAULT_DRAIN_TIMEOUT: Final[float] = 300.0
MAINTENANCE_WINDOW_DURATION: Final[timedelta] = timedelta(minutes=10)
AUDIT_AGENTS: Final[tuple[str, ...]] = (
    "data-quality",
    "model-performance",
    "retraining",
    "deployment",
)


@dataclass(frozen=True)
class WorldState:
    """The first simulated business day and the last day completed."""

    first: date
    last_done: date


@dataclass(frozen=True)
class StallEvent:
    id: str
    ts: datetime


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
    """Resolves (run_dir, world_dir, data_dir) from a run name or path.

    Fails with the path if run.env cannot be found or read, without fallback guessing.
    """
    p = Path(run)
    run_dir: Path | None = None
    if p.is_dir() and (p / newrun.RUN_FILE).is_file():
        run_dir = p
    elif p.is_file() and p.name == newrun.RUN_FILE:
        run_dir = p.parent
    elif not p.exists():
        try:
            folder = newrun.run_folder(str(run))
            if (folder / newrun.RUN_FILE).is_file():
                run_dir = folder
            else:
                raise FileNotFoundError(f"run.env not found at {folder / newrun.RUN_FILE}")
        except newrun.RunError as exc:
            raise FileNotFoundError(f"run.env not found at {p / newrun.RUN_FILE}") from exc
    else:
        raise FileNotFoundError(f"run.env not found at {p / newrun.RUN_FILE}")

    env_path = run_dir / newrun.RUN_FILE
    if not env_path.is_file():
        raise FileNotFoundError(f"run.env not found at {env_path}")

    settings = _read_env_settings(env_path)
    if paths.WORLD_VARIABLE not in settings:
        raise ValueError(f"Missing {paths.WORLD_VARIABLE} in {env_path}")
    if paths.DATA_VARIABLE not in settings:
        raise ValueError(f"Missing {paths.DATA_VARIABLE} in {env_path}")

    world_dir = Path(settings[paths.WORLD_VARIABLE])
    data_dir = Path(settings[paths.DATA_VARIABLE])
    if not world_dir.exists():
        raise FileNotFoundError(f"World directory not found at {world_dir}")
    if not data_dir.exists():
        raise FileNotFoundError(f"Data directory not found at {data_dir}")

    return run_dir, world_dir, data_dir


def _get_app_commit() -> str:
    try:
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
        return commit if commit else "unknown"
    except Exception:
        return "unknown"


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


def _has_handoff_and_finished_turn(sess: object) -> tuple[bool, str | None]:
    busy_obj = _extract_field(sess, "busy")
    if bool(busy_obj):
        return False, "session is busy"

    msgs_obj = _extract_field(sess, "messages")
    msgs = cast(list[object], msgs_obj) if isinstance(msgs_obj, list) else []

    if any(_extract_string_field(m, "status") in ("running", "queued") for m in msgs):
        return False, "has running or queued message"

    handoff_idx: int | None = None
    for i, m in enumerate(msgs):
        author_name = _extract_string_field(m, "author_name")
        from_agent = _extract_string_field(m, "from_agent")
        if author_name == "Handoff" or from_agent == "Handoff":
            handoff_idx = i
            break
        parts = _extract_field(m, "parts")
        if isinstance(parts, list):
            for part in cast(list[object], parts):
                text = _extract_string_field(part, "text")
                if text and "[handoff " in text:
                    handoff_idx = i
                    break
        if handoff_idx is not None:
            break

    if handoff_idx is None:
        return False, "waiting for handoff message"

    after_msgs = msgs[handoff_idx + 1 :]
    if not after_msgs:
        return False, "waiting for finished turn after handoff"

    finished = any(
        _extract_string_field(m, "status") not in ("running", "queued") for m in after_msgs
    )
    if not finished:
        return False, "waiting for finished turn after handoff"

    return True, None


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


def _extract_stall_events(activity_page: object) -> list[StallEvent]:
    entries_obj = _extract_field(activity_page, "entries")
    entries = cast(list[object], entries_obj) if isinstance(entries_obj, list) else []
    stalls: list[StallEvent] = []
    for entry in entries:
        ev = _extract_field(entry, "event")
        if ev is None:
            continue
        ev_type = _extract_string_field(ev, "type")
        ev_id = _extract_string_field(ev, "id")
        if ev_type == "turn.stalled" and ev_id:
            ts_str = _extract_string_field(ev, "ts")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    ts = datetime.now(UTC)
            else:
                ts = datetime.now(UTC)
            stalls.append(StallEvent(id=ev_id, ts=ts))
    return stalls


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


def _serialize_score(sc: Score) -> dict[str, Any]:
    return {
        "matched": {
            inc_id: {
                "kind": p.kind,
                "category": str(p.category),
                "start": p.start.isoformat(),
                "end": p.end.isoformat(),
                "columns": list(p.columns),
            }
            for inc_id, p in sc.matched.items()
        },
        "verdicts": {
            inc_id: v.model_dump(mode="json", exclude_none=True)
            for inc_id, v in sc.verdicts.items()
        },
        "misses": [
            {
                "kind": p.kind,
                "category": str(p.category),
                "start": p.start.isoformat(),
                "end": p.end.isoformat(),
                "columns": list(p.columns),
            }
            for p in sc.misses
        ],
        "silent": {res_id: str(v) for res_id, v in sc.silent.items()},
        "excluded": list(sc.excluded),
    }


def _write_fallback_report(
    run_dir: Path,
    info: RunInfo,
    sc: Score,
    score_data: dict[str, Any],
) -> None:
    rep_title = f"{info.mode.title()} {info.scenario} on {info.dataset}"
    json_data: dict[str, Any] = {
        "title": rep_title,
        "info": {
            "title": rep_title,
            "mode": info.mode,
            "dataset": info.dataset,
            "scenario": info.scenario,
            "seed": info.seed,
            "app_commit": info.app_commit,
            "started": info.started.isoformat(),
            "ended": info.ended.isoformat(),
            "provider": info.provider,
            "model": info.model,
            "seconds_per_day": info.seconds_per_day,
            "pauses": [
                {
                    "reason": p.reason,
                    "start": p.start.isoformat(),
                    "end": p.end.isoformat(),
                    "duration_s": (p.end - p.start).total_seconds(),
                }
                for p in info.pauses
            ],
            "day_gaps": [d.isoformat() for d in info.day_gaps],
        },
        "score": score_data,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    md_lines = [
        f"# {rep_title}\n",
        f"- Mode: {info.mode}",
        f"- Commit: {info.app_commit}",
        f"- Matched: {len(sc.matched)}",
        f"- Misses: {len(sc.misses)}",
    ]
    (run_dir / "report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


async def maintain_hold_windows(
    client: BenchClient,
    project_id: str,
    now: datetime,
    *,
    duration: timedelta = MAINTENANCE_WINDOW_DURATION,
    start: datetime | None = None,
) -> None:
    """Keeps a maintenance window open across all expectation refs during a hold."""
    raw_exp: object = await client.get(f"/projects/{project_id}/expectations")
    scope = _extract_expectation_refs(raw_exp)
    if not scope:
        return

    win_start = start if start is not None else now
    body: dict[str, Any] = {
        "start": win_start.isoformat(),
        "end": (now + duration).isoformat(),
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
    what is being waited for, nothing else. Route errors count as not ready.
    """
    # 1. Handoff exists
    try:
        handoff: object = await client.get(f"/projects/{project_id}/handoff")
    except Exception as exc:
        return False, f"route error: {exc}"
    if handoff is None:
        return False, "waiting for handoff"

    # 2. Check the four audit agents
    for ag_name in AUDIT_AGENTS:
        try:
            sess: object = await client.get(f"/projects/{project_id}/agents/{ag_name}/session")
        except Exception as exc:
            return False, f"route error: {exc}"
        has_finished, reason = _has_handoff_and_finished_turn(sess)
        if not has_finished:
            return False, f"waiting for agent {ag_name} ({reason})"

    # 3. Check other chat agents are not active
    try:
        agents_list: object = await client.get(f"/projects/{project_id}/agents")
    except Exception as exc:
        return False, f"route error: {exc}"
    if isinstance(agents_list, list):
        for ag in cast(list[object], agents_list):
            ag_name = _extract_string_field(ag, "name")
            if not ag_name or ag_name in AUDIT_AGENTS:
                continue
            chat_val = _extract_field(ag, "chat")
            if chat_val is False:
                continue
            try:
                sess = await client.get(f"/projects/{project_id}/agents/{ag_name}/session")
            except Exception as exc:
                return False, f"route error: {exc}"
            if _is_session_active(sess):
                return False, f"waiting for agent {ag_name} session to finish"

    # 4. No approval is pending
    try:
        approvals: object = await client.get(f"/projects/{project_id}/approvals")
    except Exception as exc:
        return False, f"route error: {exc}"
    if isinstance(approvals, list) and len(cast(list[object], approvals)) > 0:
        return False, f"waiting for {len(cast(list[object], approvals))} pending approval(s)"

    # 5. No agent PR is open
    try:
        gh_items: object = await client.get(f"/projects/{project_id}/github/activity")
    except Exception as exc:
        return False, f"route error: {exc}"
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
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    run_dir: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stdout: TextIO = sys.stdout,
) -> Score:
    """Drives an agents benchmark run in order:

    1. Wait before day 1 until ready, holding world and maintaining windows.
    2. Play business days, creating hold on model stall and resuming when stall approvals clear.
    3. Maintain maintenance windows every renewal interval during every hold.
    4. Drain agents after last day until sessions are idle.
    5. Score and report after the last simulated day completes.
    """
    resolved_run_dir, world_dir, data_dir = resolve_run_paths(run)
    final_run_dir = run_dir if run_dir is not None else resolved_run_dir
    hold_file = world_dir / simulate.HOLD_FILE

    run_started = clock()
    pauses: list[Pause] = []

    # Pace-derived renewal interval and maintenance window duration:
    # Renewal interval is min(30.0, pace / 4.0), and window duration is twice renewal interval.
    if seconds_per_day is not None and seconds_per_day > 0:
        renewal_interval = min(30.0, seconds_per_day / 4.0)
    else:
        renewal_interval = min(30.0, poll_interval)
    window_duration = timedelta(seconds=2 * renewal_interval)
    effective_maint_interval = min(maintenance_interval, renewal_interval)

    state_file = world_dir / simulate.STATE_FILE
    if not state_file.is_file():
        raise FileNotFoundError(f"state.json not found at {state_file}")
    if read_world_state(world_dir) is None:
        raise ValueError(f"World state file {state_file} could not be read or is invalid")

    try:
        # --- Phase 1: Wait before day 1 ---
        wait_start = clock()
        last_maint_time: datetime | None = None
        last_printed_reason: str | None = None

        while True:
            now = clock()
            if (
                last_maint_time is None
                or (now - last_maint_time).total_seconds() >= effective_maint_interval
            ):
                try:
                    await maintain_hold_windows(client, project_id, now, duration=window_duration)
                    last_maint_time = now
                except Exception as exc:
                    if is_route_error(exc):
                        stdout.write(
                            f"Route error maintaining readiness windows: {exc}; retrying...\n"
                        )
                        stdout.flush()
                    else:
                        raise

            is_ready, waiting_reason = await check_readiness(client, project_id)
            if is_ready:
                break

            if waiting_reason != last_printed_reason and waiting_reason is not None:
                stdout.write(f"{waiting_reason}\n")
                stdout.flush()
                last_printed_reason = waiting_reason

            await sleep(renewal_interval)

        wait_end = clock()
        pauses.append(Pause(reason="ready_wait", start=wait_start, end=wait_end))
        if hold_file.exists():
            with contextlib.suppress(OSError):
                hold_file.unlink()

        # Seed seen stall events at end of readiness wait so onboarding stalls are not counted as pauses.
        seen_stall_event_ids: set[str] = set()
        while True:
            try:
                initial_stalls_page: object = await client.get(
                    f"/projects/{project_id}/activity", params={"type": "turn.stalled"}
                )
                seen_stall_event_ids = {s.id for s in _extract_stall_events(initial_stalls_page)}
                break
            except Exception as exc:
                if is_route_error(exc):
                    stdout.write(f"Route error seeding stalls: {exc}; retrying...\n")
                    stdout.flush()
                    await sleep(renewal_interval)
                    continue
                raise
        warned_user_verdict_ids: set[str] = set()

        # --- Phase 2: Play, holding on a stall ---
        while True:
            state = read_world_state(world_dir)
            if state is not None and state.last_done >= end_date:
                break

            # Poll for early user verdicts
            try:
                raw_verdicts = await client.get(f"/projects/{project_id}/verdicts")
                if isinstance(raw_verdicts, list):
                    for v in cast(list[object], raw_verdicts):
                        if _extract_string_field(v, "source") == "user":
                            inc_id = _extract_string_field(v, "incident_id")
                            if inc_id and inc_id not in warned_user_verdict_ids:
                                warned_user_verdict_ids.add(inc_id)
                                stdout.write(
                                    f"Warning: person's verdict detected on incident {inc_id}\n"
                                )
                                stdout.flush()
            except Exception as exc:
                stdout.write(f"Warning: error polling verdicts: {exc}; retrying...\n")
                stdout.flush()

            stall_detected = False
            stall_event_ts: datetime | None = None
            try:
                activity_page = await client.get(
                    f"/projects/{project_id}/activity", params={"type": "turn.stalled"}
                )
            except Exception as exc:
                if is_route_error(exc):
                    stdout.write(f"Route error polling activity: {exc}; retrying...\n")
                    stdout.flush()
                    await sleep(renewal_interval)
                    continue
                raise

            new_stalls = [
                s for s in _extract_stall_events(activity_page) if s.id not in seen_stall_event_ids
            ]
            if new_stalls:
                for s in new_stalls:
                    seen_stall_event_ids.add(s.id)
                stall_detected = True
                stall_event_ts = min(s.ts for s in new_stalls)

            try:
                approvals = await client.get(f"/projects/{project_id}/approvals")
            except Exception as exc:
                if is_route_error(exc):
                    stdout.write(f"Route error polling approvals: {exc}; retrying...\n")
                    stdout.flush()
                    await sleep(renewal_interval)
                    continue
                raise

            if _filter_stall_approvals(approvals):
                stall_detected = True

            if stall_detected:
                stall_start = stall_event_ts if stall_event_ts is not None else clock()
                with contextlib.suppress(OSError):
                    hold_file.touch(exist_ok=True)

                stall_window_posted = False
                last_stall_maint: datetime | None = None
                # First window starts at stall event time (can be in past)
                try:
                    await maintain_hold_windows(
                        client, project_id, clock(), duration=window_duration, start=stall_start
                    )
                    stall_window_posted = True
                    last_stall_maint = clock()
                except Exception as exc:
                    if is_route_error(exc):
                        stdout.write(f"Route error maintaining windows: {exc}; retrying...\n")
                        stdout.flush()
                    else:
                        raise

                while True:
                    now = clock()
                    needs_maint = (
                        not stall_window_posted
                        or last_stall_maint is None
                        or (now - last_stall_maint).total_seconds() >= renewal_interval
                    )
                    if needs_maint:
                        try:
                            maint_start = stall_start if not stall_window_posted else None
                            await maintain_hold_windows(
                                client,
                                project_id,
                                now,
                                duration=window_duration,
                                start=maint_start,
                            )
                            stall_window_posted = True
                            last_stall_maint = now
                        except Exception as exc:
                            if is_route_error(exc):
                                stdout.write(
                                    f"Route error maintaining windows: {exc}; retrying...\n"
                                )
                                stdout.flush()
                            else:
                                raise

                    # A stall raised while a hold is already on joins that hold: mark its event seen
                    try:
                        cur_act = await client.get(
                            f"/projects/{project_id}/activity", params={"type": "turn.stalled"}
                        )
                        for s in _extract_stall_events(cur_act):
                            seen_stall_event_ids.add(s.id)
                    except Exception as exc:
                        if is_route_error(exc):
                            stdout.write(
                                f"Route error polling stall activity: {exc}; retrying...\n"
                            )
                            stdout.flush()
                        else:
                            raise

                    try:
                        cur_approvals = await client.get(f"/projects/{project_id}/approvals")
                    except Exception as exc:
                        if is_route_error(exc):
                            stdout.write(
                                f"Route error polling stall approvals: {exc}; retrying...\n"
                            )
                            stdout.flush()
                            await sleep(renewal_interval)
                            continue
                        raise

                    if not _filter_stall_approvals(cur_approvals):
                        stall_end = clock()
                        pauses.append(Pause(reason="model_stall", start=stall_start, end=stall_end))
                        if hold_file.exists():
                            with contextlib.suppress(OSError):
                                hold_file.unlink()
                        break

                    await sleep(renewal_interval)

            await sleep(renewal_interval)

        # --- Phase 3: Drain agents after last day ---
        drain_deadline = clock() + timedelta(seconds=drain_timeout)
        drain_timed_out = False

        while True:
            any_busy = False
            try:
                agents_list_obj = await client.get(f"/projects/{project_id}/agents")
                if isinstance(agents_list_obj, list):
                    for ag in cast(list[object], agents_list_obj):
                        ag_name = _extract_string_field(ag, "name")
                        chat_val = _extract_field(ag, "chat")
                        if chat_val is False or not ag_name:
                            continue
                        sess = await client.get(f"/projects/{project_id}/agents/{ag_name}/session")
                        if _is_session_active(sess):
                            any_busy = True
                            break
            except Exception as exc:
                if is_route_error(exc):
                    stdout.write(f"Route error draining agents: {exc}; retrying...\n")
                    stdout.flush()
                    any_busy = True
                else:
                    raise

            stalls_open = False
            try:
                cur_approvals = await client.get(f"/projects/{project_id}/approvals")
                if _filter_stall_approvals(cur_approvals):
                    stalls_open = True
            except Exception as exc:
                if is_route_error(exc):
                    stdout.write(f"Route error draining approvals: {exc}; retrying...\n")
                    stdout.flush()
                    stalls_open = True
                else:
                    raise

            if not any_busy and not stalls_open:
                break

            if clock() >= drain_deadline:
                drain_timed_out = True
                stdout.write("Warning: agent drain timed out before all sessions were idle\n")
                stdout.flush()
                break

            await sleep(renewal_interval)

        # --- Phase 4: Score and report ---
        state_file = world_dir / simulate.STATE_FILE
        if not state_file.is_file():
            raise FileNotFoundError(f"state.json not found at {state_file}")
        state = read_world_state(world_dir)
        if state is None:
            raise ValueError(f"World state file {state_file} could not be read or is invalid")

        # 1. Read metadata BEFORE score()
        agents_list = await client.get(f"/projects/{project_id}/agents")
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

        # 2. Check for person's verdicts before scoring; refuse with judged incidents after saving metadata
        user_judged_incidents: list[str] = []
        try:
            raw_verdicts = await client.get(f"/projects/{project_id}/verdicts")
            if isinstance(raw_verdicts, list):
                for v in cast(list[object], raw_verdicts):
                    if _extract_string_field(v, "source") == "user":
                        inc_id = _extract_string_field(v, "incident_id")
                        if inc_id and inc_id not in user_judged_incidents:
                            user_judged_incidents.append(inc_id)
        except Exception as exc:
            meta_dict = {
                "mode": "agents",
                "dataset": "hotel-demo",
                "scenario": scenario_name,
                "app_commit": app_commit,
                "started": run_started.isoformat(),
                "ended": clock().isoformat(),
                "provider": provider,
                "model": model,
                "seconds_per_day": seconds_per_day,
                "error": f"Failed checking verdicts: {exc}",
            }
            final_run_dir.mkdir(parents=True, exist_ok=True)
            (final_run_dir / "metadata.json").write_text(
                json.dumps(meta_dict, indent=2), encoding="utf-8"
            )
            raise RuntimeError(f"Failed to check verdicts before scoring: {exc}") from exc

        if user_judged_incidents:
            meta_dict = {
                "mode": "agents",
                "dataset": "hotel-demo",
                "scenario": scenario_name,
                "app_commit": app_commit,
                "started": run_started.isoformat(),
                "ended": clock().isoformat(),
                "provider": provider,
                "model": model,
                "seconds_per_day": seconds_per_day,
                "user_judged_incidents": sorted(user_judged_incidents),
            }
            final_run_dir.mkdir(parents=True, exist_ok=True)
            (final_run_dir / "metadata.json").write_text(
                json.dumps(meta_dict, indent=2), encoding="utf-8"
            )
            judged_str = ", ".join(sorted(user_judged_incidents))
            raise AlreadyScored(
                f"Cannot score: user verdicts recorded for incident(s): {judged_str}"
            )

        # 3. Read answer key via demo-world's reader
        key_file = world_dir / answer_key.ANSWER_KEY_FILE
        if not key_file.is_file():
            raise FileNotFoundError(f"Answer key not found at {key_file}")
        try:
            raw_lines = answer_key.lines(world_dir)
        except Exception as exc:
            raise ValueError(f"Failed to read answer key from {key_file}: {exc}") from exc

        records: list[Record] = []
        for d in raw_lines:
            kind = d.get("kind")
            if not isinstance(kind, str):
                raise ValueError(
                    f"Malformed answer key line in {key_file}: missing 'kind' in {d!r}"
                )
            if kind in ("ci_failure_set", "ci_failure_restored", "pr_merged", "pr_not_merged"):
                continue
            if "category" not in d or "start" not in d or "end" not in d:
                raise ValueError(f"Malformed answer key record in {key_file}: {d!r}")
            col_val = d.get("column")
            col_str = str(col_val) if col_val is not None else None
            records.append(
                Record(
                    kind=kind,
                    category=cast(answer_key.Category, d["category"]),
                    column=col_str,
                    start=str(d["start"]),
                    end=str(d["end"]),
                )
            )

        if not records:
            raise ValueError(f"Answer key at {key_file} holds no problem records")

        jobs_dir = data_dir / "jobs"
        starts, gaps = day_starts_from_jobs(jobs_dir, date_range=(state.first, state.last_done))

        # 4. Score
        score_res = await score(client, project_id, records, starts=starts)

        # 5. Write score result immediately to disk
        score_dict = _serialize_score(score_res)
        final_run_dir.mkdir(parents=True, exist_ok=True)
        (final_run_dir / "score_result.json").write_text(
            json.dumps(score_dict, indent=2), encoding="utf-8"
        )
        (final_run_dir / "score.json").write_text(
            json.dumps(score_dict, indent=2), encoding="utf-8"
        )

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

        try:
            await write_report(client, project_id, final_run_dir, info, score_res)
        except Exception as exc:
            log.warning("write_report failed: %s; writing fallback report", exc)
            _write_fallback_report(final_run_dir, info, score_res, score_dict)

        if drain_timed_out:
            rep_json_path = final_run_dir / "report.json"
            if rep_json_path.exists():
                try:
                    rep_dict = json.loads(rep_json_path.read_text(encoding="utf-8"))
                    if "info" in rep_dict and isinstance(rep_dict["info"], dict):
                        rep_dict["info"]["drain_timeout"] = True
                    rep_dict["drain_timeout"] = True
                    rep_json_path.write_text(json.dumps(rep_dict, indent=2), encoding="utf-8")
                except Exception as exc:
                    stdout.write(
                        f"Warning: failed to record drain timeout in {rep_json_path}: {exc}\n"
                    )
                    stdout.flush()
            rep_md_path = final_run_dir / "report.md"
            if rep_md_path.exists():
                try:
                    rep_md_text = rep_md_path.read_text(encoding="utf-8")
                    rep_md_text += (
                        "\n> **Warning:** agent drain timed out before all sessions were idle.\n"
                    )
                    rep_md_path.write_text(rep_md_text, encoding="utf-8")
                except Exception as exc:
                    stdout.write(
                        f"Warning: failed to record drain timeout in {rep_md_path}: {exc}\n"
                    )
                    stdout.flush()

        return score_res
    finally:
        if hold_file.exists():
            with contextlib.suppress(OSError):
                hold_file.unlink()


async def agents_run_cli(
    *,
    run: str | Path,
    project_id: str,
    base_url: str,
    end_date: date = DEFAULT_END_DATE,
    scenario_name: str = "agents-short",
    seconds_per_day: float | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
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
        drain_timeout=drain_timeout,
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
        "--drain-timeout",
        type=float,
        default=DEFAULT_DRAIN_TIMEOUT,
        help="Timeout in seconds to wait for agent sessions to drain (default: 300.0)",
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
                drain_timeout=float(args.drain_timeout),
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
    "AUDIT_AGENTS",
    "DEFAULT_DRAIN_TIMEOUT",
    "DEFAULT_END_DATE",
    "DEFAULT_MAINTENANCE_INTERVAL",
    "DEFAULT_POLL_INTERVAL",
    "ROUTE_ERRORS",
    "StallEvent",
    "WorldState",
    "agents_run_cli",
    "check_readiness",
    "drive_agents_run",
    "is_route_error",
    "maintain_hold_windows",
    "read_world_state",
    "register_agents_run_cli",
    "resolve_run_paths",
    "run_agents_run_cli",
]
