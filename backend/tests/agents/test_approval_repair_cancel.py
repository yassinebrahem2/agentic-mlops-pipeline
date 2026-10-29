"""The restart repair is a task of the project's recovery, not of the request that started it: a
cancelled first caller does not stop it, a close cuts it without waiting for an observer, and an
observer that calls back into the sessions does not wait on it."""

from __future__ import annotations

import asyncio

import pytest

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.runtime.sessions import SessionManager
from tests.agents.repair_doubles import HeldObserver
from tests.agents.repair_env import RepairEnv


async def test_a_cancelled_first_caller_leaves_the_repair_running(repair: RepairEnv) -> None:
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)
    app = repair.restart()
    held = HeldObserver([approval.id])
    app.store.add_observer(held)
    stream = await repair.stream(app)

    first_use = asyncio.create_task(app.manager.ensure_recovered(repair.project_id))
    await held.wait_entered(approval.id)
    first_use.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_use
    held.release[approval.id].set()

    assert await repair.ended(stream) == (turn_id, "done")
    assert (await repair.card(approval)).status == "approved"
    assert (await repair.stored(approval.id)).settled_at is not None
    assert await repair.audit_of(approval.id) == [("user", "approved")]
    assert await repair.events_of(approval.id) == [("user", "approved")]
    assert repair.ends(turn_id) == ["done"]


async def test_close_cuts_a_held_repair_and_the_next_start_repairs_again(
    repair: RepairEnv,
) -> None:
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)
    app = repair.restart()
    held = HeldObserver([approval.id])
    app.store.add_observer(held)
    first_use = asyncio.create_task(app.manager.ensure_recovered(repair.project_id))
    await held.wait_entered(approval.id)

    await asyncio.wait_for(app.manager.close(), timeout=10)

    assert held.cancelled == [approval.id]
    with pytest.raises(asyncio.CancelledError):
        await first_use
    assert (await repair.stored(approval.id)).settled_at is None

    again = repair.restart()
    stream = await repair.stream(again)
    await again.manager.ensure_recovered(repair.project_id)

    assert await repair.ended(stream) == (turn_id, "done")
    assert again.observer.calls == [("decided", approval.id, "approved")]
    assert (await repair.card(approval)).status == "approved"
    assert await repair.audit_of(approval.id) == [("user", "approved")]
    assert repair.ends(turn_id) == ["done"]


class HeldResume(SessionManager):
    """Once held, holds every resume_turn before it acts until released; records a cancel."""

    entered: asyncio.Event
    release: asyncio.Event
    cancelled: bool

    def hold(self) -> None:
        self.entered, self.release, self.cancelled = asyncio.Event(), asyncio.Event(), False

    async def resume_turn(self, project_id: str, session_id: str, turn_id: str) -> None:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        await super().resume_turn(project_id, session_id, turn_id)


async def test_close_project_before_the_repair_resumes_leaves_no_worker(
    repair: RepairEnv,
) -> None:
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)
    app = repair.restart(HeldResume)
    manager = app.manager
    assert isinstance(manager, HeldResume)
    manager.hold()
    first_use = asyncio.create_task(manager.ensure_recovered(repair.project_id))
    await asyncio.wait_for(manager.entered.wait(), timeout=10)

    await manager.close_project(repair.project_id)

    assert manager.cancelled
    manager.release.set()
    with pytest.raises(asyncio.CancelledError):
        await first_use
    assert not await repair.running(app)
    assert await repair.turn_status(turn_id) == "waiting"
    assert (repair.ends(turn_id), repair.waits(turn_id)) == ([], 0)


class PostingObserver:
    """Like onboarding's kickoff: a decision posts a message to another agent."""

    def __init__(self, manager: SessionManager, project_id: str) -> None:
        self._manager = manager
        self._project_id = project_id
        self.posted: list[str] = []

    async def asked(self, view: ApprovalView) -> None:
        return None

    async def decided(self, view: ApprovalView) -> None:
        message = await self._manager.post(
            self._project_id,
            "orchestrator",
            author_name="writer",
            from_agent="writer",
            text="Kickoff",
            exchange_id=None,
            hop=None,
        )
        self.posted.append(message.id)


async def test_an_observer_posting_from_the_repair_does_not_wait_on_the_recovery(
    repair: RepairEnv,
) -> None:
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)
    app = repair.restart()
    posting = PostingObserver(app.manager, repair.project_id)
    app.store.add_observer(posting)
    stream = await repair.stream(app)

    await asyncio.wait_for(app.manager.ensure_recovered(repair.project_id), timeout=10)

    assert len(posting.posted) == 1
    assert await repair.ended(stream) == (turn_id, "done")
