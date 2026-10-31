"""The restart repair (approvals-fixes task 2) composed with the real proposals observer, the
real Propose tool and proposals' own `recover`, on the app `build_services` builds. The split of
recovery work: proposals' `recover` (at startup) applies a proposal `approved` with no result;
the repair's re-tell applies a proposal still `pending` whose approval is decided."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Select, func, select, update

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import ToolResultPart
from mlops_agents.agents.tools.propose import PROPOSE_TOOL_NAME
from mlops_agents.api import services as api_services
from mlops_agents.api.services import Services
from mlops_agents.clock import now_iso
from mlops_agents.proposals.contract import CONNECTIONS_KIND, ApplyResult
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.storage.tables import approvals, audit, events, turns
from tests.api.conftest import add_local_project, started_app
from tests.fakes.fake_openai import RunningFake
from tests.proposals.samples import connections
from tests.proposals.test_observer import ScriptedTestKind

AGENT = "orchestrator"


def registered_kind(monkeypatch: pytest.MonkeyPatch) -> ScriptedTestKind:
    """A connections kind whose apply is recorded, registered by every app's build_services."""
    kind = ScriptedTestKind(CONNECTIONS_KIND, slots=[])

    def scripted_connections_kind(*args: Any, **kwargs: Any) -> ScriptedTestKind:
        return kind

    # Recovery runs inside build_services, before a test could swap the kind: build_services
    # registers the scripted kind as its connections kind instead.
    monkeypatch.setattr(api_services, "ConnectionsKind", scripted_connections_kind)
    return kind


def give_propose(services: Services) -> None:
    """The agent may call the real Propose tool (no shipped definition lists it yet)."""
    agent = services.catalog.get(AGENT)
    services.catalog._by_name[AGENT] = agent.model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"tools": (*agent.tools, PROPOSE_TOOL_NAME)}
    )


async def ask(services: Services, project_id: str, fake: RunningFake) -> tuple[str, str]:
    """The agent proposes a connection; its turn and approval once the turn waits."""
    provider = await services.providers.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await services.providers.add_model(
        ModelCreate(provider=provider.name, wire_model_id="fake-tools")
    )
    await services.providers.verify_model("fake:fake-tools")
    await services.agent_settings.set_default_model(AGENT, "fake:fake-tools")
    fake.state.scripted_tool_calls = [
        {
            "name": PROPOSE_TOOL_NAME,
            "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
        }
    ]
    await services.sessions.send(project_id, AGENT, "Propose a connection")
    async with asyncio.timeout(15):
        while True:
            waiting = await services.approvals.pending(project_id)
            if waiting:
                fake.state.scripted_tool_calls = None  # the resumed turn ends after the tool
                return waiting[0].turn_id, waiting[0].id
            await asyncio.sleep(0.05)


async def scalar(services: Services, project_id: str, query: Select[Any]) -> Any:
    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        return (await conn.execute(query)).scalar_one()


async def restarted_turn_ends(services: Services, project_id: str, turn_id: str) -> list[str]:
    """The restarted app's first use repairs; how the turn ended, each time, once it ended."""
    await services.approvals.pending(project_id)
    async with asyncio.timeout(15):
        while True:
            found = await services.events.history(project_id, types=["turn.ended"])
            ends = [str(e.payload["status"]) for e in found if e.subject_id == turn_id]
            if ends:
                await asyncio.sleep(0.2)  # a second end, if any, would land here
                found = await services.events.history(project_id, types=["turn.ended"])
                return [str(e.payload["status"]) for e in found if e.subject_id == turn_id]
            await asyncio.sleep(0.05)


async def propose_output(services: Services, project_id: str, turn_id: str) -> list[str]:
    """What the Propose tool returned to the turn, each time it ran."""
    view = await services.sessions.get(project_id, AGENT)
    return [
        part.content
        for message in view.messages
        if message.turn_id == turn_id
        for part in message.parts
        if isinstance(part, ToolResultPart)
    ]


async def records(services: Services, project_id: str, approval_id: str) -> tuple[int, int]:
    """(audit records, approval.decided events) of the approval."""
    audits = select(func.count()).where(
        audit.c.action == "approval",
        func.json_extract(audit.c.detail, "$.approval_id") == approval_id,
    )
    decided = select(func.count()).where(
        events.c.type == "approval.decided", events.c.subject_id == approval_id
    )
    return (
        await scalar(services, project_id, audits),
        await scalar(services, project_id, decided),
    )


async def test_a_crash_inside_apply_is_applied_once_by_recover_and_resumed_once(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = registered_kind(monkeypatch)
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        give_propose(services)
        project_id = await add_local_project(app, tmp_path, git_env)
        turn_id, approval_id = await ask(services, project_id, fake_openai)

        async def dies(project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
            raise asyncio.CancelledError  # the process dies inside apply

        kind.on_apply = dies
        with pytest.raises(asyncio.CancelledError):
            await services.approvals.decide(project_id, approval_id, approve=True, note=None)
        proposal = await services.proposals.get_by_approval(project_id, approval_id)
        assert proposal is not None and (proposal.status, proposal.result) == ("approved", None)
    kind.on_apply = None
    kind.apply_calls.clear()

    async with started_app(tmp_path / "data") as app:
        services = app.state.services
        give_propose(services)
        assert await restarted_turn_ends(services, project_id, turn_id) == ["done"]

        assert len(kind.apply_calls) == 1  # by proposals' recover, at startup
        proposal = await services.proposals.get_by_approval(project_id, approval_id)
        assert proposal is not None and proposal.result == ApplyResult(
            applied=["conn_applied"], failed={}
        )
        assert await propose_output(services, project_id, turn_id) == [
            "Approved. Everything was applied."
        ]
        assert await records(services, project_id, approval_id) == (1, 1)


async def test_a_crash_after_the_approval_commit_is_applied_once_by_the_retell(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = registered_kind(monkeypatch)
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        give_propose(services)
        project_id = await add_local_project(app, tmp_path, git_env)
        turn_id, approval_id = await ask(services, project_id, fake_openai)
        # What the status commit of an approve leaves when the process dies before any observer
        # ran: the approval approved by the user, not settled; the proposal still pending.
        engine = await services.databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(approvals)
                .where(approvals.c.id == approval_id)
                .values(status="approved", decided_at=now_iso(), decided_by="user")
            )
    kind.apply_calls.clear()

    async with started_app(tmp_path / "data") as app:
        restarted: Services = app.state.services
        give_propose(restarted)
        order: list[str] = []

        async def status() -> str:
            query = select(turns.c.status).where(turns.c.id == turn_id)
            return str(await scalar(restarted, project_id, query))

        async def applied(project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
            # A slow apply: a turn resumed before its observer finished leaves `waiting` here.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(0.5):
                    while True:
                        if await status() != "waiting":
                            break
                        await asyncio.sleep(0.02)
            order.append(f"apply while {await status()}")
            return ApplyResult(applied=["conn_applied"], failed={})

        kind.on_apply = applied  # recover, at startup, leaves a pending proposal alone
        proposal = await restarted.proposals.get_by_approval(project_id, approval_id)
        assert proposal is not None and proposal.status == "pending"  # recover leaves it

        assert await restarted_turn_ends(restarted, project_id, turn_id) == ["done"]

        assert len(kind.apply_calls) == 1  # by the re-told decision's observer
        assert order == ["apply while waiting"]  # the turn resumed after the observer finished
        assert await propose_output(restarted, project_id, turn_id) == [
            "Approved. Everything was applied."
        ]
        stored = await ApprovalStore(restarted.databases).get(project_id, approval_id)
        assert stored is not None and stored.settled_at is not None
        proposal = await restarted.proposals.get_by_approval(project_id, approval_id)
        assert proposal is not None and proposal.status == "approved"
        assert await records(restarted, project_id, approval_id) == (1, 1)
