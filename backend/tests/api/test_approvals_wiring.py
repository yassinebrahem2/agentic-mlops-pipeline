"""The one approval store (I13): `build_services` builds it and every approval transition passes
through it, so an observer registered once on the service sees them all."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.api.services import Services
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.storage.tables import sessions, turns
from tests.agents.approval_env import RecordingObserver
from tests.api.conftest import add_local_project, started_app
from tests.fakes.fake_openai import RunningFake

SRC = Path(__file__).resolve().parents[2] / "src"


def test_only_build_services_constructs_an_approval_store() -> None:
    sites = [
        f"{path.relative_to(SRC).as_posix()}:{n}"
        for path in sorted(SRC.rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r"\bApprovalStore\(", line)
    ]

    assert len(sites) == 1, sites
    assert sites[0].startswith("mlops_agents/api/services.py:")


async def _pending(services: Services, project_id: str, count: int) -> list[str]:
    async with asyncio.timeout(15):
        while True:
            pending = await services.approvals.pending(project_id)
            if len(pending) >= count:
                return [a.id for a in pending]
            await asyncio.sleep(0.05)


async def _waiting(services: Services, project_id: str) -> None:
    """Until the orchestrator's turn waits for its approval (Stop then stops a waiting turn)."""
    async with asyncio.timeout(15):
        while True:
            if (await services.sessions.turn_states(project_id)).get("orchestrator") == "waiting":
                return
            await asyncio.sleep(0.05)


async def _use_model(services: Services, fake: RunningFake, wire_model_id: str) -> None:
    provider = await services.providers.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await services.providers.add_model(
        ModelCreate(provider=provider.name, wire_model_id=wire_model_id)
    )
    await services.providers.verify_model(f"fake:{wire_model_id}")
    await services.agent_settings.set_default_model("orchestrator", f"fake:{wire_model_id}")


@pytest.mark.asyncio
async def test_an_observer_sees_a_gate_approval_asked_and_cancelled_by_stop(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        observer = RecordingObserver()
        services.approvals.add_observer(observer)
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-write")

        # fake-write starts a task first; at the default level StartTask asks.
        await services.sessions.send(project_id, "orchestrator", "Start a task")
        [approval_id] = await _pending(services, project_id, 1)
        # The turn waits only after the gate has created the approval and told the observers.
        await _waiting(services, project_id)
        assert observer.calls == [("asked", approval_id, "pending")]

        await services.sessions.stop(project_id, "orchestrator")

        assert observer.calls == [
            ("asked", approval_id, "pending"),
            ("decided", approval_id, "cancelled"),
        ]


@pytest.mark.asyncio
async def test_an_observer_sees_recovery_expire_a_stale_approval(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        # A pending approval left by a turn that is no longer waiting, as after a crash.
        engine = await services.databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                sessions.insert().values(
                    id="ses_1",
                    agent="orchestrator",
                    summary=None,
                    compacted_through=None,
                    created_at="2026-10-28T10:00:00Z",
                )
            )
            await conn.execute(
                turns.insert().values(
                    id="trn_1",
                    session_id="ses_1",
                    trigger_message_id="msg_trigger",
                    status="interrupted",
                    error=None,
                    created_at="2026-10-28T10:00:00Z",
                    started_at="2026-10-28T10:00:00Z",
                    ended_at="2026-10-28T10:01:00Z",
                )
            )
        stale = await ApprovalStore(services.databases).create(
            project_id,
            turn_id="trn_1",
            session_id="ses_1",
            message_id="msg_1",
            agent="orchestrator",
            tool_call_id="call_1",
            tool="edit_file",
            risk_class=RiskClass.WRITE_CODE,
            preview=Preview(kind="diff", title="Edit", body="+x"),
        )
        observer = RecordingObserver()
        services.approvals.add_observer(observer)

        # The project's first use recovers it.
        assert await services.approvals.pending(project_id) == []

        assert observer.calls == [("decided", stale.id, "expired")]


@pytest.mark.asyncio
async def test_cancelling_the_last_pending_approval_of_a_turn_resumes_it(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")
        # Two calls in one step: at the default level each StartTask asks.
        fake_openai.state.scripted_tool_calls = [
            {"name": "StartTask", "arguments": {"title": "First"}},
            {"name": "StartTask", "arguments": {"title": "Second"}},
        ]
        await services.sessions.send(project_id, "orchestrator", "Start two tasks")
        first, second = await _pending(services, project_id, 2)
        await _waiting(services, project_id)
        [turn_id] = {a.turn_id for a in await services.approvals.pending(project_id)}

        await services.approvals.cancel(project_id, first)

        assert await _turn_status(services, project_id, turn_id) == "waiting"
        assert [a.id for a in await services.approvals.pending(project_id)] == [second]

        await services.approvals.cancel(project_id, second)

        # Resumed: both calls come back as rejected, the model answers and the turn ends.
        async with asyncio.timeout(15):
            while True:
                if await _turn_status(services, project_id, turn_id) == "done":
                    break
                await asyncio.sleep(0.05)
        assert await services.approvals.pending(project_id) == []


async def _turn_status(services: Services, project_id: str, turn_id: str) -> str:
    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        status = (
            await conn.execute(select(turns.c.status).where(turns.c.id == turn_id))
        ).scalar_one()
    return str(status)


async def test_wakeups_start_only_after_the_approvals_service_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting wake-ups fires the due ones, which can recover projects and expire approvals; an
    observer registered later on the service would miss those transitions."""
    from mlops_agents.agents.approvals.service import ApprovalsService
    from mlops_agents.agents.messaging.wakeups import WakeupService

    order: list[str] = []
    approvals_init = ApprovalsService.__init__
    wakeups_start = WakeupService.start

    def recording_init(self: ApprovalsService, *args: Any, **kwargs: Any) -> None:
        order.append("approvals built")
        approvals_init(self, *args, **kwargs)

    async def recording_start(self: WakeupService) -> asyncio.Task[None]:
        order.append("wakeups started")
        return await wakeups_start(self)

    monkeypatch.setattr(ApprovalsService, "__init__", recording_init)
    monkeypatch.setattr(WakeupService, "start", recording_start)

    async with started_app(tmp_path):
        pass

    assert order == ["approvals built", "wakeups started"]
