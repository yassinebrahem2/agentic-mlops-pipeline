"""Tests for the bench agents-run command and workflow."""

from __future__ import annotations

import argparse
import asyncio
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
    AUDIT_AGENTS,
    WorldState,
    check_readiness,
    drive_agents_run,
    maintain_hold_windows,
    read_world_state,
    register_agents_run_cli,
    resolve_run_paths,
)
from benchmarks.client import (
    BenchClient,
    BenchHttpError,
    in_process,
)
from benchmarks.fake_clock import FakeClock
from benchmarks.scorer import AlreadyScored, Score

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

    world_dir = tmp_path / "world"
    world_dir.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / newrun.RUN_FILE
    env_file.write_text(
        f"{paths.WORLD_VARIABLE}={world_dir.resolve()}\n{paths.DATA_VARIABLE}={data_dir.resolve()}\n",
        encoding="utf-8",
    )

    async with app.router.lifespan_context(app):
        services: Services = app.state.services
        await services.scheduler.stop()
        project = await services.workspace.add_local(str(repo_dir))
        client = in_process(app, token)
        yield client, services, project.id


class ReadyAuditClient:
    """Wraps BenchClient so the 4 audit agent sessions report handoff received and finished turn."""

    def __init__(self, inner: BenchClient) -> None:
        self.inner = inner

    async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        for ag in AUDIT_AGENTS:
            if path.endswith(f"/agents/{ag}/session"):
                return {
                    "busy": False,
                    "messages": [
                        {
                            "author_name": "Handoff",
                            "from_agent": "Handoff",
                            "status": "complete",
                            "parts": [{"text": "[handoff prp_1]"}],
                        },
                        {
                            "author_name": ag,
                            "status": "complete",
                            "parts": [{"text": f"Audit of {ag} completed"}],
                        },
                    ],
                }
        if "/session" in path:
            return {"busy": False, "messages": []}
        return await self.inner.get(path, params)

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        return await self.inner.post(path, body)


def test_read_world_state_and_resolve_paths(tmp_path: Path) -> None:
    world_dir = tmp_path / "world"
    world_dir.mkdir()
    assert read_world_state(world_dir) is None

    state_file = world_dir / simulate.STATE_FILE
    state_file.write_text('{"first": "2016-07-01"}\n', encoding="utf-8")
    assert read_world_state(world_dir) is None

    state_file.write_text('{"first": "2016-07-01", "last_done": "2016-07-05"}\n', encoding="utf-8")
    ws = read_world_state(world_dir)
    assert ws is not None
    assert ws == WorldState(first=date(2016, 7, 1), last_done=date(2016, 7, 5))

    run_dir = tmp_path / "run_folder"
    run_dir.mkdir()
    (run_dir / "world").mkdir()
    (run_dir / "data").mkdir()
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    # No fallback guessing: missing run.env stops with that path
    with pytest.raises(FileNotFoundError, match=r"run\.env"):
        resolve_run_paths(run_dir)

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
        ready_client = ReadyAuditClient(client)

        # 1. No handoff -> waiting for handoff
        ready, reason = await check_readiness(cast(BenchClient, ready_client), project_id)
        assert not ready
        assert reason == "waiting for handoff"

        # 2. Store handoff
        handoff = FixtureDataset().handoff()
        await services.handoffs.store(project_id, "prp_handoff", handoff)

        # Now handoff exists and audit sessions have finished turn -> ready!
        ready, reason = await check_readiness(cast(BenchClient, ready_client), project_id)
        assert ready
        assert reason is None

        # 3. Test detailed session and approval blockers
        class MockClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner
                self.busy_agent: str | None = None
                self.missing_audit_turn: str | None = None
                self.pending_approvals: list[dict[str, Any]] = []
                self.github_items: list[dict[str, Any]] = []

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if self.busy_agent and f"/agents/{self.busy_agent}/session" in path:
                    return {"busy": True, "messages": []}
                if self.missing_audit_turn and f"/agents/{self.missing_audit_turn}/session" in path:
                    return {"busy": False, "messages": []}
                if path.endswith("/approvals"):
                    return list(self.pending_approvals)
                if path.endswith("/github/activity"):
                    return list(self.github_items)
                return await ready_client.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        mock = MockClient(client)

        # Audit agent has not finished turn
        mock.missing_audit_turn = "data-quality"
        ready, reason = await check_readiness(cast(BenchClient, mock), project_id)
        assert not ready
        assert reason == "waiting for agent data-quality (waiting for handoff message)"
        mock.missing_audit_turn = None

        # Agent busy
        mock.busy_agent = "onboarding"
        ready, reason = await check_readiness(cast(BenchClient, mock), project_id)
        assert not ready
        assert reason == "waiting for agent onboarding session to finish"
        mock.busy_agent = None

        # Approval pending
        mock.pending_approvals = [{"id": "appr_1", "kind": "command"}]
        ready, reason = await check_readiness(cast(BenchClient, mock), project_id)
        assert not ready
        assert reason == "waiting for 1 pending approval(s)"
        mock.pending_approvals = []

        # Open PR
        mock.github_items = [{"kind": "pr", "state": "open", "number": 42}]
        ready, reason = await check_readiness(cast(BenchClient, mock), project_id)
        assert not ready
        assert reason == "waiting for open PR #42"
        mock.github_items = []

        # All clear -> ready!
        ready, reason = await check_readiness(cast(BenchClient, mock), project_id)
        assert ready
        assert reason is None


@pytest.mark.asyncio
async def test_turn_stalled_creates_hold_and_two_stalls_resolution(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        hold_file = world_dir / simulate.HOLD_FILE

        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )

        ready_client = ReadyAuditClient(client)

        class FakeBenchClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner
                self.stalls: list[dict[str, Any]] = []
                self.activity: list[dict[str, Any]] = []

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if path.endswith("/approvals"):
                    return list(self.stalls)
                if path.endswith("/activity") and params and params.get("type") == "turn.stalled":
                    return {"entries": list(self.activity)}
                return await ready_client.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        fake_client = FakeBenchClient(client)
        hold_file.touch()
        call_count = 0

        async def controlled_sleep(_duration: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Stall occurs during play
                fake_client.stalls = [{"kind": "stall", "id": "s1"}, {"kind": "stall", "id": "s2"}]
                fake_client.activity = [
                    {
                        "event": {
                            "id": "ev_stall_play",
                            "type": "turn.stalled",
                            "ts": "2016-07-01T08:00:00Z",
                        }
                    }
                ]
            elif call_count in (2, 3):
                assert hold_file.exists()
                fake_client.stalls.pop(0)
            elif call_count == 4:
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
        assert not hold_file.exists()

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
        spec = ExpectationSpec(
            key=ExpectationKey(kind="job", ref="daily_job"),
            category="operational",
            cadence=Cadence(cron=None, every_minutes=10),
            timezone="UTC",
            grace_minutes=5,
            source="handoff",
        )
        await services.expectations.register(project_id, spec)

        for _ in range(12):
            await maintain_hold_windows(client, project_id, clock.now())
            clock.advance(timedelta(minutes=5))

        windows = await services.maintenance_windows.list(project_id)
        assert len(windows) == 12
        assert all("daily_job" in w.scope for w in windows)

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

        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

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

        ready_client = ReadyAuditClient(client)

        score_res = await drive_agents_run(
            client=cast(BenchClient, ready_client),
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 2),
            poll_interval=0.01,
            clock=clock.now,
        )

        assert isinstance(score_res, Score)
        rep_md = tmp_path / "report.md"
        rep_json = tmp_path / "report.json"
        score_res_json = tmp_path / "score_result.json"
        assert rep_md.exists()
        assert rep_json.exists()
        assert score_res_json.exists()

        data = json.loads(rep_json.read_text(encoding="utf-8"))
        assert data["info"]["mode"] == "agents"
        assert data["info"]["scenario"] == "agents-short"
        assert len(data["info"]["pauses"]) >= 1
        assert data["info"]["pauses"][0]["reason"] == "ready_wait"


@pytest.mark.asyncio
async def test_failure_after_score_leaves_score_result_and_report_on_disk(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )

        ready_client = ReadyAuditClient(client)

        class FailingWriteReportClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/stats" in path:
                    raise RuntimeError("503 Stats service down")
                return await ready_client.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        fail_client = FailingWriteReportClient(client)

        score_res = await drive_agents_run(
            client=cast(BenchClient, fail_client),
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 1),
            poll_interval=0.01,
            clock=clock.now,
        )

        assert isinstance(score_res, Score)
        # Result and reports must exist on disk despite write_report failing!
        assert (tmp_path / "score_result.json").exists()
        assert (tmp_path / "score.json").exists()
        assert (tmp_path / "report.json").exists()
        assert (tmp_path / "report.md").exists()


@pytest.mark.asyncio
async def test_person_verdict_during_play_warns_and_refuses_scoring(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-02.jsonl").write_text(
            '{"started": "2016-07-02T08:00:00Z"}\n', encoding="utf-8"
        )
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-01"}\n',
            encoding="utf-8",
        )

        ready_client = ReadyAuditClient(client)
        stdout_buf = io.StringIO()

        class UserVerdictClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if path.endswith("/verdicts"):
                    return [
                        {
                            "incident_id": "inc_user_123",
                            "source": "user",
                            "value": "real_issue",
                        }
                    ]
                return await ready_client.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        uv_client = UserVerdictClient(client)

        async def step_sleep(_d: float) -> None:
            (world_dir / simulate.STATE_FILE).write_text(
                '{"first": "2016-07-01", "last_done": "2016-07-02"}\n', encoding="utf-8"
            )

        with pytest.raises(AlreadyScored) as exc_info:
            await drive_agents_run(
                client=cast(BenchClient, uv_client),
                project_id=project_id,
                run=tmp_path,
                end_date=date(2016, 7, 2),
                poll_interval=0.01,
                stdout=stdout_buf,
                sleep=step_sleep,
                clock=clock.now,
            )

        err_msg = str(exc_info.value)
        assert "inc_user_123" in err_msg
        assert (
            "Warning: person's verdict detected on incident inc_user_123" in stdout_buf.getvalue()
        )
        assert (tmp_path / "metadata.json").exists()


@pytest.mark.asyncio
async def test_stall_raised_during_onboarding_is_not_a_pause(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )

        ready_client = ReadyAuditClient(client)

        class OnboardingStallClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if path.endswith("/activity") and params and params.get("type") == "turn.stalled":
                    # Stall that happened during onboarding
                    return {
                        "entries": [
                            {
                                "event": {
                                    "id": "ev_onboarding_stall",
                                    "type": "turn.stalled",
                                    "ts": "2016-07-01T07:50:00Z",
                                }
                            }
                        ]
                    }
                return await ready_client.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        ob_client = OnboardingStallClient(client)

        score_res = await drive_agents_run(
            client=cast(BenchClient, ob_client),
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 1),
            poll_interval=0.01,
            clock=clock.now,
        )

        assert isinstance(score_res, Score)
        rep_json = tmp_path / "report.json"
        rep_data = json.loads(rep_json.read_text(encoding="utf-8"))
        pauses = rep_data["info"]["pauses"]
        reasons = [p["reason"] for p in pauses]
        assert "ready_wait" in reasons
        # Onboarding stall must NOT be recorded as a model stall pause
        assert "model_stall" not in reasons


@pytest.mark.asyncio
async def test_stall_window_starts_at_its_event_time(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 10, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        spec = ExpectationSpec(
            key=ExpectationKey(kind="job", ref="daily_job"),
            category="operational",
            cadence=Cadence(cron=None, every_minutes=10),
            timezone="UTC",
            grace_minutes=5,
            source="handoff",
        )
        await services.expectations.register(project_id, spec)
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        world_dir = tmp_path / "world"
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T10:00:00Z"}\n', encoding="utf-8"
        )

        stall_event_time = datetime(2016, 7, 1, 9, 55, tzinfo=UTC)

        class StallDriverClient(ReadyAuditClient):
            def __init__(self, inner: BenchClient) -> None:
                super().__init__(inner)
                self.stalled = False
                self.approvals_list: list[dict[str, Any]] = []

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/activity" in path and params and params.get("type") == "turn.stalled":
                    if self.stalled:
                        return {
                            "entries": [
                                {
                                    "event": {
                                        "id": "ev_stall_1",
                                        "type": "turn.stalled",
                                        "ts": stall_event_time.isoformat(),
                                    }
                                }
                            ]
                        }
                    return {"entries": []}
                if "/approvals" in path:
                    return list(self.approvals_list)
                return await super().get(path, params)

        fake_client = StallDriverClient(client)
        call_count = 0

        async def controlled_sleep(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            clock.advance(timedelta(seconds=duration + 0.1))
            if call_count == 1:
                fake_client.stalled = True
                fake_client.approvals_list = [{"id": "app_stall_1", "kind": "stall"}]
            elif call_count == 2:
                fake_client.approvals_list = []
            elif call_count >= 3:
                (world_dir / simulate.STATE_FILE).write_text(
                    '{"first": "2016-07-01", "last_done": "2016-07-02"}\n', encoding="utf-8"
                )
                (jobs_dir / "2016-07-02.jsonl").write_text(
                    '{"started": "2016-07-02T10:00:00Z"}\n', encoding="utf-8"
                )

        await drive_agents_run(
            client=cast(BenchClient, fake_client),
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 2),
            poll_interval=0.01,
            sleep=controlled_sleep,
            clock=clock.now,
        )

        windows = await services.maintenance_windows.list(project_id)
        stall_windows = [w for w in windows if w.start == stall_event_time]
        assert len(stall_windows) >= 1
        assert stall_windows[0].start == stall_event_time


@pytest.mark.asyncio
async def test_after_resume_no_window_outlives_twice_renewal_interval(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        spec = ExpectationSpec(
            key=ExpectationKey(kind="job", ref="daily_job"),
            category="operational",
            cadence=Cadence(cron=None, every_minutes=10),
            timezone="UTC",
            grace_minutes=5,
            source="handoff",
        )
        await services.expectations.register(project_id, spec)
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        world_dir = tmp_path / "world"
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )

        class ResumeDriverClient(ReadyAuditClient):
            def __init__(self, inner: BenchClient) -> None:
                super().__init__(inner)
                self.stalled = False
                self.approvals_list: list[dict[str, Any]] = []

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/activity" in path and params and params.get("type") == "turn.stalled":
                    if self.stalled:
                        return {
                            "entries": [
                                {
                                    "event": {
                                        "id": "ev_stall_resume",
                                        "type": "turn.stalled",
                                        "ts": "2016-07-01T08:05:00Z",
                                    }
                                }
                            ]
                        }
                    return {"entries": []}
                if "/approvals" in path:
                    return list(self.approvals_list)
                return await super().get(path, params)

        fake_client = ResumeDriverClient(client)
        call_count = 0
        resume_time: datetime | None = None
        seconds_per_day = 60.0

        async def controlled_sleep(duration: float) -> None:
            nonlocal call_count, resume_time
            call_count += 1
            clock.advance(timedelta(seconds=duration + 0.1))
            if call_count == 1:
                fake_client.stalled = True
                fake_client.approvals_list = [{"id": "app_stall_resume", "kind": "stall"}]
            elif call_count in (2, 3):
                pass
            elif call_count == 4:
                fake_client.approvals_list = []
                resume_time = clock.now()
            elif call_count >= 5:
                (world_dir / simulate.STATE_FILE).write_text(
                    '{"first": "2016-07-01", "last_done": "2016-07-02"}\n', encoding="utf-8"
                )
                (jobs_dir / "2016-07-02.jsonl").write_text(
                    '{"started": "2016-07-02T08:00:00Z"}\n', encoding="utf-8"
                )

        await drive_agents_run(
            client=cast(BenchClient, fake_client),
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 2),
            seconds_per_day=seconds_per_day,
            sleep=controlled_sleep,
            clock=clock.now,
        )

        assert resume_time is not None
        windows = await services.maintenance_windows.list(project_id)
        renewal_interval = min(30.0, seconds_per_day / 4.0)
        max_allowed_end = resume_time + timedelta(seconds=2 * renewal_interval)
        for w in windows:
            assert w.end <= max_allowed_end


@pytest.mark.asyncio
async def test_stall_during_existing_hold_joins_hold_one_pause(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())
        world_dir = tmp_path / "world"
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )

        class MultiStallClient(ReadyAuditClient):
            def __init__(self, inner: BenchClient) -> None:
                super().__init__(inner)
                self.events: list[dict[str, Any]] = []
                self.approvals_list: list[dict[str, Any]] = []

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/activity" in path and params and params.get("type") == "turn.stalled":
                    return {"entries": [{"event": e} for e in self.events]}
                if "/approvals" in path:
                    return list(self.approvals_list)
                return await super().get(path, params)

        fake_client = MultiStallClient(client)
        call_count = 0

        async def controlled_sleep(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            clock.advance(timedelta(seconds=duration + 0.1))
            if call_count == 1:
                # Stall A happens
                fake_client.events.append(
                    {"id": "ev_stall_A", "type": "turn.stalled", "ts": "2016-07-01T08:05:00Z"}
                )
                fake_client.approvals_list.append({"id": "app_A", "kind": "stall"})
            elif call_count == 2:
                # Stall B happens while hold is already on
                fake_client.events.append(
                    {"id": "ev_stall_B", "type": "turn.stalled", "ts": "2016-07-01T08:06:00Z"}
                )
                fake_client.approvals_list.append({"id": "app_B", "kind": "stall"})
            elif call_count == 3:
                # Stall A approval resolved, Stall B still pending
                fake_client.approvals_list = [
                    a for a in fake_client.approvals_list if a["id"] != "app_A"
                ]
            elif call_count == 4:
                # Stall B approval resolved
                fake_client.approvals_list = []
            elif call_count >= 5:
                (world_dir / simulate.STATE_FILE).write_text(
                    '{"first": "2016-07-01", "last_done": "2016-07-02"}\n', encoding="utf-8"
                )
                (jobs_dir / "2016-07-02.jsonl").write_text(
                    '{"started": "2016-07-02T08:00:00Z"}\n', encoding="utf-8"
                )

        await drive_agents_run(
            client=cast(BenchClient, fake_client),
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 2),
            poll_interval=0.01,
            sleep=controlled_sleep,
            clock=clock.now,
        )

        rep_json = tmp_path / "report.json"
        rep_data = json.loads(rep_json.read_text(encoding="utf-8"))
        model_pauses = [p for p in rep_data["info"]["pauses"] if p["reason"] == "model_stall"]
        assert len(model_pauses) == 1


@pytest.mark.asyncio
async def test_route_error_during_play_retried_and_run_succeeds(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())
        world_dir = tmp_path / "world"
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )

        class PlayRouteErrorClient(ReadyAuditClient):
            def __init__(self, inner: BenchClient) -> None:
                super().__init__(inner)
                self.failed_once = False

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/activity" in path and not self.failed_once:
                    self.failed_once = True
                    raise BenchHttpError(path, 500)
                return await super().get(path, params)

        fake_client = PlayRouteErrorClient(client)
        call_count = 0

        async def controlled_sleep(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            clock.advance(timedelta(seconds=duration + 0.1))
            if call_count >= 2:
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
            sleep=controlled_sleep,
            clock=clock.now,
        )
        assert score_res is not None
        assert fake_client.failed_once


@pytest.mark.asyncio
async def test_non_route_error_or_cancellation_during_hold_leaves_no_hold_file(
    tmp_path: Path,
) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())
        world_dir = tmp_path / "world"
        hold_file = world_dir / simulate.HOLD_FILE
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )

        class StallCrashClient(ReadyAuditClient):
            def __init__(self, inner: BenchClient) -> None:
                super().__init__(inner)
                self.stalled = False

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/activity" in path and params and params.get("type") == "turn.stalled":
                    if self.stalled:
                        return {
                            "entries": [
                                {
                                    "event": {
                                        "id": "ev_stall_crash",
                                        "type": "turn.stalled",
                                        "ts": "2016-07-01T08:05:00Z",
                                    }
                                }
                            ]
                        }
                    return {"entries": []}
                if "/approvals" in path:
                    if self.stalled:
                        # Non-route error during hold!
                        raise RuntimeError("Unexpected disk failure")
                    return []
                return await super().get(path, params)

        fake_client = StallCrashClient(client)
        call_count = 0

        async def controlled_sleep(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            clock.advance(timedelta(seconds=duration + 0.1))
            if call_count == 1:
                fake_client.stalled = True

        with pytest.raises(RuntimeError, match="Unexpected disk failure"):
            await drive_agents_run(
                client=cast(BenchClient, fake_client),
                project_id=project_id,
                run=tmp_path,
                end_date=date(2016, 7, 2),
                poll_interval=0.01,
                sleep=controlled_sleep,
                clock=clock.now,
            )

        assert not hold_file.exists()


@pytest.mark.asyncio
async def test_cancellation_during_hold_leaves_no_hold_file(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())
        world_dir = tmp_path / "world"
        hold_file = world_dir / simulate.HOLD_FILE
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )

        class StallCancelClient(ReadyAuditClient):
            def __init__(self, inner: BenchClient) -> None:
                super().__init__(inner)
                self.stalled = False

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/activity" in path and params and params.get("type") == "turn.stalled":
                    if self.stalled:
                        return {
                            "entries": [
                                {
                                    "event": {
                                        "id": "ev_stall_cancel",
                                        "type": "turn.stalled",
                                        "ts": "2016-07-01T08:05:00Z",
                                    }
                                }
                            ]
                        }
                    return {"entries": []}
                if "/approvals" in path:
                    if self.stalled:
                        raise asyncio.CancelledError()
                    return []
                return await super().get(path, params)

        fake_client = StallCancelClient(client)
        call_count = 0

        async def controlled_sleep(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            clock.advance(timedelta(seconds=duration + 0.1))
            if call_count == 1:
                fake_client.stalled = True

        with pytest.raises(asyncio.CancelledError):
            await drive_agents_run(
                client=cast(BenchClient, fake_client),
                project_id=project_id,
                run=tmp_path,
                end_date=date(2016, 7, 2),
                poll_interval=0.01,
                sleep=controlled_sleep,
                clock=clock.now,
            )

        assert not hold_file.exists()


@pytest.mark.asyncio
async def test_verdict_check_failure_before_score_saves_metadata_and_fails_loudly(
    tmp_path: Path,
) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())
        world_dir = tmp_path / "world"
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-01"}\n',
            encoding="utf-8",
        )
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )

        class VerdictCheckFailClient(ReadyAuditClient):
            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/verdicts" in path:
                    raise RuntimeError("DB connection dropped")
                return await super().get(path, params)

        fake_client = VerdictCheckFailClient(client)

        with pytest.raises(RuntimeError, match="Failed to check verdicts before scoring"):
            await drive_agents_run(
                client=cast(BenchClient, fake_client),
                project_id=project_id,
                run=tmp_path,
                end_date=date(2016, 7, 1),
                poll_interval=0.01,
                clock=clock.now,
            )

        meta_file = tmp_path / "metadata.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        assert "error" in meta
        assert "DB connection dropped" in meta["error"]


@pytest.mark.asyncio
async def test_missing_state_file_stops_the_run(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())
        world_dir = tmp_path / "world"
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-01"}\n',
            encoding="utf-8",
        )
        # Ensure state.json is missing
        state_file = world_dir / simulate.STATE_FILE
        if state_file.exists():
            state_file.unlink()

        ready_client = ReadyAuditClient(client)
        with pytest.raises(FileNotFoundError, match=r"state\.json"):
            await drive_agents_run(
                client=cast(BenchClient, ready_client),
                project_id=project_id,
                run=tmp_path,
                end_date=date(2016, 7, 1),
                poll_interval=0.01,
                clock=clock.now,
            )


@pytest.mark.asyncio
async def test_missing_answer_key_stops_the_run(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())
        world_dir = tmp_path / "world"
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        # Ensure answer key does not exist
        key_file = world_dir / answer_key.ANSWER_KEY_FILE
        if key_file.exists():
            key_file.unlink()

        ready_client = ReadyAuditClient(client)
        with pytest.raises(FileNotFoundError, match=r"answer_key\.jsonl"):
            await drive_agents_run(
                client=cast(BenchClient, ready_client),
                project_id=project_id,
                run=tmp_path,
                end_date=date(2016, 7, 1),
                poll_interval=0.01,
                clock=clock.now,
            )


@pytest.mark.asyncio
async def test_answer_key_without_problem_records_stops_the_run(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())
        world_dir = tmp_path / "world"
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        # Answer key with only PR decision, no problem records
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "pr_merged", "pr": 1, "branch": "fix", "opened": "2016-07-01", "date": "2016-07-01", "delay_days": 0, "merge_after_days": 1, "reason": null}\n',
            encoding="utf-8",
        )

        ready_client = ReadyAuditClient(client)
        with pytest.raises(ValueError, match="holds no problem records"):
            await drive_agents_run(
                client=cast(BenchClient, ready_client),
                project_id=project_id,
                run=tmp_path,
                end_date=date(2016, 7, 1),
                poll_interval=0.01,
                clock=clock.now,
            )


@pytest.mark.asyncio
async def test_route_error_during_readiness_keeps_waiting(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, _services, project_id):

        class RouteErrorClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner
                self.fail = True

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if self.fail and "/handoff" in path:
                    raise RuntimeError("500 Internal Server Error")
                return await self.inner.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        err_client = RouteErrorClient(client)
        ready, reason = await check_readiness(cast(BenchClient, err_client), project_id)
        assert not ready
        assert reason is not None
        assert "route error: 500 Internal Server Error" in reason


@pytest.mark.asyncio
async def test_malformed_answer_key_line_stops_the_run(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        # Malformed answer key record: missing start/end
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike"}\n', encoding="utf-8"
        )

        ready_client = ReadyAuditClient(client)

        with pytest.raises(ValueError, match="Malformed answer key"):
            await drive_agents_run(
                client=cast(BenchClient, ready_client),
                project_id=project_id,
                run=tmp_path,
                end_date=date(2016, 7, 1),
                poll_interval=0.01,
                clock=clock.now,
            )


@pytest.mark.asyncio
async def test_scoring_waits_for_agents_still_in_a_turn_and_records_timeout(tmp_path: Path) -> None:
    clock = FakeClock(start=datetime(2016, 7, 1, 8, 0, tzinfo=UTC))
    async with _setup_test_app(tmp_path, clock) as (client, services, project_id):
        world_dir = tmp_path / "world"
        world_dir.mkdir(parents=True, exist_ok=True)
        jobs_dir = tmp_path / "data" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        (world_dir / simulate.STATE_FILE).write_text(
            '{"first": "2016-07-01", "last_done": "2016-07-01"}\n', encoding="utf-8"
        )
        (jobs_dir / "2016-07-01.jsonl").write_text(
            '{"started": "2016-07-01T08:00:00Z"}\n', encoding="utf-8"
        )
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-02"}\n',
            encoding="utf-8",
        )

        ready_client = ReadyAuditClient(client)

        class BusyDuringDrainClient:
            def __init__(self, inner: BenchClient) -> None:
                self.inner = inner
                self.calls = 0

            async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
                if "/agents/deployment/session" in path:
                    self.calls += 1
                    if self.calls > 1:
                        msg_handoff: dict[str, Any] = {
                            "author_name": "Handoff",
                            "status": "complete",
                            "parts": [],
                        }
                        msg_running: dict[str, Any] = {
                            "author_name": "deployment",
                            "status": "running",
                            "parts": [],
                        }
                        res_dict: dict[str, Any] = {
                            "busy": True,
                            "messages": [msg_handoff, msg_running],
                        }
                        return res_dict
                return await ready_client.get(path, params)

            async def post(self, path: str, body: dict[str, Any]) -> Any:
                return await self.inner.post(path, body)

        busy_client = BusyDuringDrainClient(client)

        async def advance_sleep(duration: float) -> None:
            clock.advance(timedelta(seconds=duration + 0.1))

        score_res = await drive_agents_run(
            client=cast(BenchClient, busy_client),
            project_id=project_id,
            run=tmp_path,
            end_date=date(2016, 7, 1),
            poll_interval=0.01,
            drain_timeout=0.01,
            sleep=advance_sleep,
            clock=clock.now,
        )

        assert isinstance(score_res, Score)
        rep_json = tmp_path / "report.json"
        rep_data = json.loads(rep_json.read_text(encoding="utf-8"))
        assert rep_data.get("drain_timeout") is True


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
            "--drain-timeout",
            "120",
        ]
    )
    assert args.run == "test_run"
    assert args.project == "prj_123"
    assert args.base_url == "http://127.0.0.1:8765"
    assert args.seconds_per_day == 60.0
    assert args.drain_timeout == 120.0
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
        (world_dir / answer_key.ANSWER_KEY_FILE).write_text(
            '{"kind": "null_spike", "category": "data_quality", "column": "lead_time", "start": "2016-07-01", "end": "2016-07-01"}\n',
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        call_count = 0

        ready_client = ReadyAuditClient(client)

        async def step_sleep(_d: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await services.handoffs.store(project_id, "prp_handoff", FixtureDataset().handoff())

        score_res = await drive_agents_run(
            client=cast(BenchClient, ready_client),
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
