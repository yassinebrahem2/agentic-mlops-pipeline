from __future__ import annotations

import json
import os
import socket
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from demo_world.answer_key import Record
from mlops_agents.api.app import create_app
from mlops_agents.config import RunMode, load_settings
from mlops_agents.monitoring.scheduler.loop import AppScheduler

from benchmarks.cli import main
from benchmarks.client import in_process
from benchmarks.datasets import DATASETS
from benchmarks.replay import replay
from benchmarks.scorer import AlreadyScored

from .fixture_dataset import FixtureDataset

DATASETS["fixture"] = FixtureDataset


@pytest.mark.asyncio
async def test_replay_fixture_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "run1"

    # Guard that no connection is made outside 127.0.0.1
    original_connect = socket.socket.connect
    connected_hosts: list[str] = []

    def guarded_connect(sock: socket.socket, address: tuple[str, int] | str) -> None:
        if isinstance(address, tuple):
            host = address[0]
            connected_hosts.append(host)
            assert host in (
                "127.0.0.1",
                "localhost",
                "::1",
            ), f"Attempted connection beyond 127.0.0.1 to {host}"
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    dataset = FixtureDataset()
    score_res = await replay(dataset, run_dir)

    # 1. Connected only to 127.0.0.1
    assert len(connected_hosts) > 0
    assert all(h in ("127.0.0.1", "localhost", "::1") for h in connected_hosts)

    # 2. Both report files exist
    rep_md = run_dir / "report.md"
    rep_json = run_dir / "report.json"
    assert rep_md.exists()
    assert rep_json.exists()

    rep_data = json.loads(rep_json.read_text(encoding="utf-8"))
    assert rep_data["info"]["mode"] == "replay"
    assert rep_data["info"]["dataset"] == "fixture"

    # 3. Read incidents directly from the API: exactly 1 incident, no silence incident raised
    settings = load_settings(
        RunMode.LOCAL,
        port=8765,
        env={
            "MLOPS_AGENTS_HOME": str(run_dir / "home"),
            "MLOPS_AGENTS_ACCESS_TOKEN": "test-token",
        },
    )
    app = create_app(settings)
    app.state.access_token = "test-token"
    async with app.router.lifespan_context(app):
        services = app.state.services
        projects = await services.workspace.list_projects()
        assert len(projects) == 1
        proj_id = projects[0].id
        client = in_process(app, "test-token")
        inc_page = await client.get(f"/projects/{proj_id}/incidents")
        inc_rows = inc_page["rows"]
        assert len(inc_rows) == 1
        assert inc_rows[0]["category"] == "data_quality"
        assert inc_rows[0]["category"] != "operational"

    assert len(score_res.matched) == 1
    (inc_id, problem) = next(iter(score_res.matched.items()))
    assert problem.category == "data_quality"
    assert problem.kind == "null_spike"
    assert score_res.verdicts[inc_id].value == "real_issue"
    assert len(score_res.misses) == 0
    assert len(score_res.excluded) == 0

    # 4. A second replay into the same folder refuses
    with pytest.raises(AlreadyScored):
        await replay(dataset, run_dir)


@pytest.mark.asyncio
async def test_replay_without_utime_stamping_raises_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that removing os.utime stamping causes false silence incidents."""

    def dummy_utime(
        _path: os.PathLike[str] | str | bytes | int,
        _times: tuple[int, int] | tuple[float, float] | None = None,
        *,
        ns: tuple[int, int] | None = None,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        pass

    monkeypatch.setattr(os, "utime", dummy_utime)
    run_dir = tmp_path / "run_no_utime"
    dataset = FixtureDataset()
    await replay(dataset, run_dir)

    settings = load_settings(
        RunMode.LOCAL,
        port=8765,
        env={
            "MLOPS_AGENTS_HOME": str(run_dir / "home"),
            "MLOPS_AGENTS_ACCESS_TOKEN": "test-token",
        },
    )
    app = create_app(settings)
    app.state.access_token = "test-token"
    async with app.router.lifespan_context(app):
        services = app.state.services
        projects = await services.workspace.list_projects()
        assert len(projects) == 1
        proj_id = projects[0].id
        client = in_process(app, "test-token")
        inc_page = await client.get(f"/projects/{proj_id}/incidents")
        inc_rows = inc_page["rows"]
        assert len(inc_rows) > 1, f"Expected more than 1 incident, got {inc_rows}"
        assert any("daily_feed" in r.get("cause_key", "") for r in inc_rows)


@pytest.mark.asyncio
async def test_replay_stops_scheduler_loop_and_steps_once_per_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that background scheduler loop is stopped and run_due is driven once per day."""
    run_due_calls = 0
    orig_run_due = AppScheduler.run_due

    async def counted_run_due(self: AppScheduler, project_id: str, at: Any) -> None:
        nonlocal run_due_calls
        run_due_calls += 1
        await orig_run_due(self, project_id, at)

    stop_called = False
    orig_stop = AppScheduler.stop

    async def tracked_stop(self: AppScheduler) -> None:
        nonlocal stop_called
        stop_called = True
        await orig_stop(self)

    monkeypatch.setattr(AppScheduler, "run_due", counted_run_due)
    monkeypatch.setattr(AppScheduler, "stop", tracked_stop)

    run_dir = tmp_path / "run_sched"
    dataset = FixtureDataset()
    await replay(dataset, run_dir)

    assert stop_called is True
    # Exactly one run_due per day (FixtureDataset has 4 days)
    assert run_due_calls == len(dataset.days())


def test_cli_replay_fixture_and_second_run_refusal(tmp_path: Path) -> None:
    run_dir = tmp_path / "cli_run"
    code1 = main(["replay", "fixture", "--run-dir", str(run_dir)])
    assert code1 == 0
    assert (run_dir / "report.md").exists()
    assert (run_dir / "report.json").exists()

    # Second run refuses
    code2 = main(["replay", "fixture", "--run-dir", str(run_dir)])
    assert code2 == 1


@pytest.mark.asyncio
async def test_replay_stamps_only_files_written_on_day_not_prior_days(tmp_path: Path) -> None:
    """Verifies that an earlier day's file keeps its mtime and is not re-stamped on later days."""

    class StampTrackingDataset(FixtureDataset):
        def write_day(self, day: date, data_dir: Path, results_dir: Path) -> None:
            super().write_day(day, data_dir, results_dir)
            if day == date(2030, 1, 7):
                (data_dir / "day1_only.txt").write_text("day 1 content", encoding="utf-8")

    run_dir = tmp_path / "run_stamp_tracking"
    dataset = StampTrackingDataset()
    await replay(dataset, run_dir)

    day1_file = run_dir / "data" / "day1_only.txt"
    assert day1_file.exists()
    day1_mtime = datetime.fromtimestamp(day1_file.stat().st_mtime, tz=UTC)
    expected_mtime = datetime(2030, 1, 7, 0, 0, 0, tzinfo=UTC)
    # The file must retain day 1's timestamp and NOT be re-stamped on day 2, 3, or 4
    assert day1_mtime == expected_mtime


async def _get_replay_incidents(run_dir: Path) -> list[dict[str, Any]]:
    settings = load_settings(
        RunMode.LOCAL,
        port=8765,
        env={
            "MLOPS_AGENTS_HOME": str(run_dir / "home"),
            "MLOPS_AGENTS_ACCESS_TOKEN": "test-token",
        },
    )
    app = create_app(settings)
    app.state.access_token = "test-token"
    async with app.router.lifespan_context(app):
        services = app.state.services
        projects = await services.workspace.list_projects()
        assert len(projects) == 1
        proj_id = projects[0].id
        client = in_process(app, "test-token")
        inc_page = await client.get(f"/projects/{proj_id}/incidents")
        return list(inc_page["rows"])


@pytest.mark.asyncio
async def test_replay_resolves_incident_after_three_day_problem_stops_firing(
    tmp_path: Path,
) -> None:
    """A 3-business-day problem gives exactly one incident, resolved after results stop firing."""
    run_dir = tmp_path / "run_3day"
    days = (
        date(2030, 1, 7),
        date(2030, 1, 8),
        date(2030, 1, 9),
        date(2030, 1, 10),
    )
    spike_days = (date(2030, 1, 7), date(2030, 1, 8), date(2030, 1, 9))
    key = (
        Record(
            kind="null_spike",
            category="data_quality",
            column="feature_a",
            start="2030-01-07",
            end="2030-01-09",
        ),
    )
    dataset = FixtureDataset(days=days, spike_days=spike_days, key=key)
    score_res = await replay(dataset, run_dir)

    assert len(score_res.matched) == 1
    (inc_id, _problem) = next(iter(score_res.matched.items()))
    assert score_res.verdicts[inc_id].value == "real_issue"
    assert len(score_res.misses) == 0

    incidents = await _get_replay_incidents(run_dir)
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["category"] == "data_quality"
    assert inc["resolved_at"] is not None
    # Resolved after results stop firing on day 4 (2030-01-10 01:00 UTC)
    assert datetime.fromisoformat(inc["resolved_at"]) == datetime(2030, 1, 10, 1, 0, 0, tzinfo=UTC)
    # Closed by scorer after verdict
    assert inc["status"] == "closed"
    assert inc["closed_at"] is not None
    assert len(inc["signal_ids"]) == 3


@pytest.mark.asyncio
async def test_replay_two_problems_nine_days_apart_give_two_incidents(
    tmp_path: Path,
) -> None:
    """Two problems on the same cause key spaced 9 days apart give two incidents."""
    run_dir = tmp_path / "run_two_problems"
    days = tuple(date(2030, 1, d) for d in range(7, 18))
    spike_days = (date(2030, 1, 7), date(2030, 1, 16))
    key = (
        Record(
            kind="null_spike",
            category="data_quality",
            column="feature_a",
            start="2030-01-07",
            end="2030-01-07",
        ),
        Record(
            kind="null_spike",
            category="data_quality",
            column="feature_a",
            start="2030-01-16",
            end="2030-01-16",
        ),
    )
    dataset = FixtureDataset(days=days, spike_days=spike_days, key=key)
    score_res = await replay(dataset, run_dir)

    incidents = await _get_replay_incidents(run_dir)
    assert len(incidents) == 2
    assert incidents[0]["id"] != incidents[1]["id"]
    assert incidents[0]["resolved_at"] is not None
    assert incidents[1]["resolved_at"] is not None
    # Both resolved and then closed by scorer
    assert incidents[0]["status"] == "closed"
    assert incidents[1]["status"] == "closed"

    assert len(score_res.matched) == 2
    assert len(score_res.misses) == 0
    for inc_id in score_res.matched:
        assert score_res.verdicts[inc_id].value == "real_issue"


@pytest.mark.asyncio
async def test_replay_incident_still_firing_at_end_of_run_stays_open(
    tmp_path: Path,
) -> None:
    """An incident still firing at the end of the run stays open."""
    run_dir = tmp_path / "run_still_firing"
    days = (
        date(2030, 1, 7),
        date(2030, 1, 8),
    )
    spike_days = (date(2030, 1, 8),)
    key = (
        Record(
            kind="null_spike",
            category="data_quality",
            column="feature_a",
            start="2030-01-08",
            end="2030-01-08",
        ),
    )
    dataset = FixtureDataset(days=days, spike_days=spike_days, key=key)
    score_res = await replay(dataset, run_dir)

    assert len(score_res.matched) == 1
    (inc_id, _problem) = next(iter(score_res.matched.items()))
    assert score_res.verdicts[inc_id].value == "real_issue"

    incidents = await _get_replay_incidents(run_dir)
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["category"] == "data_quality"
    assert inc["status"] == "open"
    assert inc["resolved_at"] is None
    assert inc["closed_at"] is None
