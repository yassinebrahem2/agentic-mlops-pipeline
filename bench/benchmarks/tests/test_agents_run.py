"""Tests for the bench agents-run command and workflow."""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from demo_world import answer_key, newrun, paths, simulate
from mlops_agents.api.app import create_app
from mlops_agents.api.services import Services
from mlops_agents.config import RunMode, load_settings
from mlops_agents.monitoring.expectations.contract import (
    Cadence,
    ExpectationKey,
    ExpectationSpec,
)

from benchmarks.agents_run import (
    WorldState,
    check_readiness,
    drive_agents_run,
    maintain_hold_windows,
    read_world_state,
    register_agents_run_cli,
    resolve_run_paths,
)
from benchmarks.client import BenchClient, in_process
from benchmarks.fake_clock import FakeClock
from benchmarks.scorer import Score

from .fixture_dataset import FixtureDataset


class _MemoryKeyStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self._data.get(ref)

    def set(self, ref: str, value: str) -> None:
        self._data[ref] = value

    def delete(self, ref: str) -> None:
        self._data.pop(ref, None)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_bin = shutil.which("git") or "git"
    subprocess.run([git_bin, "init"], cwd=path, check=True, capture_output=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [git_bin, "config", "user.name", "Test"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(  # noqa: S603
        [git_bin, "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run([git_bin, "add", "."], cwd=path, check=True, capture_output=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [git_bin, "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True
    )
    return path


@asynccontextmanager
async def _setup_test_app(
    tmp_path: Path, clock: FakeClock
) -> AsyncGenerator[tuple[BenchClient, Services, str], None]:
    home_dir = tmp_path / "app_home"
    home_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = _init_repo(tmp_path / "repo")
    token = "test-token"
    env = {
        "MLOPS_AGENTS_HOME": str(home_dir),
        "MLOPS_AGENTS_ACCESS_TOKEN": token,
    }
    settings = load_settings(RunMode.LOCAL, port=8765, env=env)
    app = create_app(settings, keystore=_MemoryKeyStore(), clock=clock)
    app.state.access_token = token

    async with app.router.lifespan_context(app):
        services: Services = app.state.services
        await services.scheduler.stop()
        project = await services.workspace.add_local(str(repo_dir))
        client = in_process(app, token)
        yield client, services, project.id


def test_read_world_state_and_resolve_paths(tmp_path: Path) -> None:
    # 1. Missing state
    world_dir = tmp_path / "world"
    world_dir.mkdir()
    assert read_world_state(world_dir) is None

    # 2. Incomplete state
    state_file = world_dir / simulate.STATE_FILE
    state_file.write_text('{"first": "2016-07-01"}\n', encoding="utf-8")
    assert read_world_state(world_dir) is None

    # 3. Valid state
    state_file.write_text('{"first": "2016-07-01", "last_done": "2016-07-05"}\n', encoding="utf-8")
    ws = read_world_state(world_dir)
    assert ws is not None
    assert ws == WorldState(first=date(2016, 7, 1), last_done=date(2016, 7, 5))

    # 4. Resolve paths with world/ directory
    run_dir = tmp_path / "run_folder"
    run_dir.mkdir()
    (run_dir / "world").mkdir()
    (run_dir / "data").mkdir()
    r, w, d = resolve_run_paths(run_dir)
    assert r == run_dir
    assert w == run_dir / "world"
    assert d == run_dir / "data"

    # 5. Resolve paths with run.env
    env_file = run_dir / newrun.RUN_FILE
    env_file.write_text(
        f"{paths.WORLD_VARIABLE}={world_dir}\n{paths.DATA_VARIABLE}={tmp_path / 'data'}\n",
        encoding="utf-8",
    )
    r, w, d = resolve_run_paths(run_dir)
    assert r == run_dir
    assert w == world_dir
    assert d == tmp_path / "data"


@pytest.mark.asyncio
async def test_hold_before_day_1_and_releases_only_when_ready(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        # 1. No handoff -> waiting for handoff
        ready, reason = await check_readiness(client, project_id)
        assert not ready
        assert reason == "waiting for handoff"

        # 2. Store handoff
        handoff = FixtureDataset().handoff()
        await services.handoffs.store(project_id, "prp_handoff", handoff)

        # Handoff exists and no blocking approvals/PRs -> ready!
        ready, reason = await check_readiness(client, project_id)
        assert ready
        assert reason is None

        # 3. Wrapping client to test waiting reasons
        class ReadinessTestClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner
                self.busy_agent: str | None = None
                self.pending_approvals: list[dict[str, Any]] = []
                self.github_items: list[dict[str, Any]] = []

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if self.busy_agent and f"/agents/{self.busy_agent}/session" in path:
                    return {"busy": True, "messages": []}
                if path.endswith("/approvals"):
                    return list(self.pending_approvals)
                if path.endswith("/github/activity"):
                    return list(self.github_items)
                return await self.inner.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        wrapped = ReadinessTestClient(client)

        # Agent busy
        wrapped.busy_agent = "onboarding"
        ready, reason = await check_readiness(cast(BenchClient, wrapped), project_id)
        assert not ready
        assert reason == "waiting for agent onboarding session to finish"
        wrapped.busy_agent = None

        # Approval pending
        wrapped.pending_approvals = [{"id": "appr_1", "kind": "command"}]
        ready, reason = await check_readiness(cast(BenchClient, wrapped), project_id)
        assert not ready
        assert reason == "waiting for 1 pending approval(s)"
        wrapped.pending_approvals = []

        # Open PR
        wrapped.github_items = [{"kind": "pr", "state": "open", "number": 42}]
        ready, reason = await check_readiness(cast(BenchClient, wrapped), project_id)
        assert not ready
        assert reason == "waiting for open PR #42"
        wrapped.github_items = []

        # All clear -> ready!
        ready, reason = await check_readiness(cast(BenchClient, wrapped), project_id)
        assert ready
        assert reason is None


@pytest.mark.asyncio
async def test_turn_stalled_creates_hold_and_two_stalls_resolution(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        hold_file = world_dir / simulate.HOLD_FILE

        # Ready state: store handoff
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        # World state: initial day
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )

        # Prepare jobs directory and answer key
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text("", encoding="utf-8")

        # Two pending stall approvals in client
        class FakeBenchClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner
                self.stalls = [{"kind": "stall", "id": "s1"}, {"kind": "stall", "id": "s2"}]
                self.activity: list[dict[str, Any]] = [
                    {"event": {"id": "ev_stall_1", "type": "turn.stalled"}}
                ]

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if path.endswith("/approvals"):
                    return list(self.stalls)
                if path.endswith("/activity") and params and params.get("type") == "turn.stalled":
                    return {"entries": list(self.activity)}
                return await self.inner.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        fake_client = FakeBenchClient(client)

        hold_file.touch()
        call_count = 0

        async def controlled_sleep(_duration: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First tick in play: stall detected, hold created.
                assert hold_file.exists()
                # Resolve first stall: second still pending!
                fake_client.stalls.pop(0)
            elif call_count == 2:
                # Still holding because second stall is pending!
                assert hold_file.exists()
                # Resolve second stall
                fake_client.stalls.pop(0)
            elif call_count == 3:
                # Mark run completed
                (world_dir / simulate.STATE_FILE).write_text(
                    '{"first": "2016-07-01", "last_done": "2016-07-02"}\n', encoding="utf-8"
                )
                (jobs_dir / "2016-07-02.jsonl").write_text(
                    '{"started": "2016-07-02T08:00:00Z"}\n', encoding="utf-8"
                )

        score_res = await drive_agents_run(
            client=cast(BenchClient, fake_client),
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 2),
            poll_interval=0.01,
            maintenance_interval=1000.0,
            sleep=controlled_sleep,
            clock=clock.now,
        )

        assert isinstance(score_res, Score)
        # Verify hold file was removed after both stalls cleared
        assert not hold_file.exists()

        # Verify report has pauses
        rep_json = tmp_path / "report.json"
        assert rep_json.exists()
        rep_data = json.loads(rep_json.read_text(encoding="utf-8"))
        pauses = rep_data["info"]["pauses"]
        pause_reasons = [p["reason"] for p in pauses]
        assert "ready_wait" in pause_reasons
        assert "model_stall" in pause_reasons


@pytest.mark.asyncio
async def test_maintenance_windows_cover_expectations_and_raise_no_silence(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 12, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        # Register an expectation with a 10-minute cadence and 5-minute grace
        spec = ExpectationSpec(
            key=ExpectationKey(kind="job", ref="daily_job"),
            category="operational",
            cadence=Cadence(cron=None, every_minutes=10),
            timezone="UTC",
            grace_minutes=5,
            source="handoff",
        )
        await services.expectations.register(project_id, spec)

        # Over a held hour (12:00 to 13:00), refresh window every 5 minutes
        for _ in range(12):
            await maintain_hold_windows(client, project_id, clock.now())
            clock.advance(timedelta(minutes=5))

        # Verify windows exist
        windows = await services.maintenance_windows.list(project_id)
        assert len(windows) == 12
        assert all("daily_job" in w.scope for w in windows)

        # At 13:00 (across the held hour), evaluate: all deadline slots fell during held windows
        signals = await services.expectations.evaluate(project_id, clock.now())
        assert len(signals) == 0, f"Expected no silence signals, got {signals}"

        states = await services.expectations.states(project_id)
        assert len(states) == 1
        assert states[0].status == "suspended"


@pytest.mark.asyncio
async def test_scoring_runs_once_after_last_day_and_report_written(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        data_dir = tmp_path / "data"
        jobs_dir = data_dir / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        # Ready state: store handoff
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        # World state: already at end date
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-02"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-02.jsonl").write_text(
            '{"started": "2016-07-02T08:00:00Z"}\n', encoding="utf-8"
        )
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "country", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )

        score_res = await drive_agents_run(
            client=client,
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 2),
            poll_interval=0.01,
            clock=clock.now,
        )

        assert isinstance(score_res, Score)
        # Verify reports generated
        rep_md = tmp_path / "report.md"
        rep_json = tmp_path / "report.json"
        assert rep_md.exists()
        assert rep_json.exists()

        data = json.loads(rep_json.read_text(encoding="utf-8"))
        assert data["info"]["mode"] == "agents"
        assert data["info"]["scenario"] == "agents-short"
        assert len(data["info"]["pauses"]) >= 1
        assert data["info"]["pauses"][0]["reason"] == "ready_wait"


def test_agents_run_cli_parser() -> None:
    parser = argparse.ArgumentParser()
    register_agents_run_cli(parser)
    args = parser.parse_args(
        [
            "--run",
            "test_run",
            "--project",
            "prj_123",
            "--base-url",
            "http://127.0.0.1:8765",
            "--seconds-per-day",
            "60",
        ]
    )
    assert args.run == "test_run"
    assert args.project == "prj_123"
    assert args.base_url == "http://127.0.0.1:8765"
    assert args.seconds_per_day == 60.0
    assert args.end == date(2016, 9, 11)


@pytest.mark.asyncio
async def test_waiting_prints_what_it_is_waiting_for_nothing_else(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text("", encoding="utf-8")

        stdout_buf = io.StringIO()
        call_count = 0

        async def step_sleep(_d: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Still no handoff -> printed waiting for handoff
                # Now store handoff
                await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        score_res = await drive_agents_run(
            client=client,
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 1),
            poll_interval=0.01,
            stdout=stdout_buf,
            sleep=step_sleep,
            clock=clock.now,
        )

        assert isinstance(score_res, Score)
        output = stdout_buf.getvalue()
        assert output == "waiting for handoff\n"


def test_run_agents_run_cli_handler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from benchmarks.agents_run import run_agents_run_cli

    fake_score = Score(matched={}, verdicts={}, misses=(), silent={}, excluded=())

    async def fake_cli(*args: Any, **kwargs: Any) -> Score:
        return fake_score

    monkeypatch.setattr("benchmarks.agents_run.agents_run_cli", fake_cli)

    parser = argparse.ArgumentParser()
    register_agents_run_cli(parser)
    args = parser.parse_args(
        [
            "--run",
            str(tmp_path),
            "--project",
            "prj_test",
            "--base-url",
            "http://127.0.0.1:8765",
        ]
    )
    code = run_agents_run_cli(args)
    assert code == 0
