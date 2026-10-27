"""The approvals service's guards: the stored task id, the store's pending-only update under a
real race, and recovery before listing or deciding (#258)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import FileAccess, Preview, RiskClass, TaskRef
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env
from tests.agents.test_approvals_service import FakeSessionsForApproval
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

PREVIEW = Preview(kind="diff", title="edit main.py", body="- old\n+ new")


class OtherTaskCurrent:
    """Task workspaces whose current task is one the approval was not asked under."""

    def __init__(self, current: TaskRef | None) -> None:
        self._current = current

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        return self._current

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        raise NotImplementedError


class BothReadBeforeEitherWrites(ApprovalStore):
    """Holds the first two reads of an approval until both have been made, so two decisions
    both see it pending and only the store's own pending-only update can pick the winner."""

    def __init__(self, databases: Databases) -> None:
        super().__init__(databases)
        self._readers = 0
        self._both_read = asyncio.Event()

    async def get(self, project_id: str, approval_id: str) -> ApprovalRecord | None:
        record = await super().get(project_id, approval_id)
        if self._readers < 2:
            self._readers += 1
            if self._readers == 2:
                self._both_read.set()
            await asyncio.wait_for(self._both_read.wait(), timeout=5)
        return record


async def _waiting_approval(
    databases: Databases,
    project_id: str,
    *,
    turn_status: str = "waiting",
    task_id: str | None = None,
) -> ApprovalRecord:
    """A session whose turn is in `turn_status`, with one pending approval in its message."""
    session_store = SessionStore(databases)
    session_id = await session_store.session_id(project_id, "orchestrator")
    turn_id = await session_store.add_turn(project_id, session_id, "msg_trigger", turn_status)
    message = await session_store.add_message(
        project_id,
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[
            ApprovalPart(
                type="approval",
                approval_id="apr_1",
                tool_call_id="call_1",
                preview=PREVIEW,
                status="pending",
                note=None,
            )
        ],
        status="waiting",
    )
    return await ApprovalStore(databases).create(
        project_id,
        id="apr_1",
        turn_id=turn_id,
        session_id=session_id,
        message_id=message.id,
        agent="orchestrator",
        tool_call_id="call_1",
        tool="edit_file",
        risk_class=RiskClass.WRITE_CODE,
        preview=PREVIEW,
        task_id=task_id,
    )


@pytest.mark.parametrize("current_task", ["tsk_other", None])
async def test_a_decision_keeps_the_task_the_approval_was_asked_under(
    databases: Databases, tmp_path: Path, current_task: str | None
) -> None:
    project_id = "prj_keeptask"
    apr = await _waiting_approval(databases, project_id, task_id="tsk_asked")
    current = (
        TaskRef(id=current_task, title="Other", branch="b", worktree=tmp_path, status="active")
        if current_task is not None
        else None
    )
    service = ApprovalsService(
        databases,
        FakeSessionsForApproval(SessionStore(databases)),
        workspaces=OtherTaskCurrent(current),
    )

    await service.decide(project_id, apr.id, approve=True)

    stored = await ApprovalStore(databases).get(project_id, apr.id)
    assert stored is not None
    assert stored.task_id == "tsk_asked"


async def test_two_decisions_that_both_see_the_approval_pending_one_wins_one_gets_409(
    databases: Databases,
) -> None:
    project_id = "prj_racedecide"
    apr = await _waiting_approval(databases, project_id)
    fake_sessions = FakeSessionsForApproval(SessionStore(databases))
    service = ApprovalsService(
        databases, fake_sessions, store=BothReadBeforeEitherWrites(databases)
    )

    results = await asyncio.gather(
        service.decide(project_id, apr.id, approve=True, note="approve"),
        service.decide(project_id, apr.id, approve=False, note="reject"),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, ApprovalView)]
    errors = [r for r in results if isinstance(r, DomainError)]
    assert len(successes) == 1
    assert [(e.code, e.status) for e in errors] == [("approval_not_pending", 409)]
    stored = await ApprovalStore(databases).get(project_id, apr.id)
    assert stored is not None
    assert stored.status == successes[0].status
    assert len(fake_sessions.resumed_turns) == 1


async def _service_after_restart(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> tuple[ApprovalsService, str, ApprovalRecord]:
    """A fresh session manager (not recovered yet) over a project the last run left with a
    turn still running and its approval pending."""
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    apr = await _waiting_approval(databases, env.project_id, turn_status="running")
    return ApprovalsService(databases, env.manager), env.project_id, apr


async def test_pending_after_a_restart_lists_no_approval_of_a_turn_that_is_not_waiting(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    service, project_id, _ = await _service_after_restart(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    assert await service.pending(project_id) == []


async def test_deciding_after_a_restart_an_approval_of_a_turn_that_is_not_waiting_gets_409(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    # No pending() first: its recovery would hide a decide that skips its own.
    service, project_id, apr = await _service_after_restart(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    with pytest.raises(DomainError) as exc_info:
        await service.decide(project_id, apr.id, approve=True)

    assert (exc_info.value.code, exc_info.value.status) == ("approval_not_pending", 409)
    stored = await ApprovalStore(databases).get(project_id, apr.id)
    assert stored is not None
    assert stored.status == "expired"
