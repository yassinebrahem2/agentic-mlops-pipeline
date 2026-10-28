"""The approvals service's guards: the stored task id, and recovery before listing or deciding
(#258)."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env
from tests.agents.test_approvals_service import FakeSessionsForApproval
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

PREVIEW = Preview(kind="diff", title="edit main.py", body="- old\n+ new")


async def waiting_approval(
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


async def test_a_decision_keeps_the_task_the_approval_was_asked_under(
    databases: Databases,
) -> None:
    project_id = "prj_keeptask"
    apr = await waiting_approval(databases, project_id, task_id="tsk_asked")
    service = ApprovalsService(
        databases, FakeSessionsForApproval(SessionStore(databases)), store=ApprovalStore(databases)
    )

    await service.decide(project_id, apr.id, approve=True)

    stored = await ApprovalStore(databases).get(project_id, apr.id)
    assert stored is not None
    assert stored.task_id == "tsk_asked"


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
    apr = await waiting_approval(databases, env.project_id, turn_status="running")
    return (
        ApprovalsService(databases, env.manager, store=env.approval_store),
        env.project_id,
        apr,
    )


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
