"""One owner writes an approval decision's audit record and `approval.decided` event, and the
three places that decide approvals (a user's decision, Stop, recovery) all go through it
(#258 item 4, Decision #278)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.agents.approvals.decisions import record_decisions
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.runtime.contract import DecidedApprovalStatus
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit
from tests.agents.runtime.session_env import build_session_env
from tests.agents.test_approval_expiry_and_stop import StoreAskGate, collect_until_turn_waiting
from tests.agents.test_approvals_service import FakeSessionsForApproval
from tests.agents.test_approvals_service_guards import waiting_approval
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

Call = tuple[str, list[str], DecidedApprovalStatus, str, str | None]


def spy_on(module: Any, monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    """Record each call `module` makes to the shared function, which still does its work."""
    calls: list[Call] = []

    async def spy(
        project_id: str,
        approvals: Sequence[ApprovalRecord],
        decision: DecidedApprovalStatus,
        *,
        actor: str,
        databases: Databases,
        audit_log: AuditLog | None,
        event_log: EventLog | None,
        note: str | None = None,
    ) -> None:
        calls.append((project_id, [a.id for a in approvals], decision, actor, note))
        await record_decisions(
            project_id,
            approvals,
            decision,
            actor=actor,
            databases=databases,
            audit_log=audit_log,
            event_log=event_log,
            note=note,
        )

    monkeypatch.setattr(module, "record_decisions", spy)
    return calls


async def _audit_rows(databases: Databases, project_id: str) -> list[dict[str, Any]]:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(audit).order_by(audit.c.id))).mappings().all()
    return [{**r, "detail": json.loads(r["detail"])} for r in rows]


@pytest.mark.parametrize(
    ("decision", "actor", "note", "detail_note"),
    [
        ("approved", "user", "Looks fine", {"note": "Looks fine"}),
        ("rejected", "user", None, {"note": None}),
        ("cancelled", "user", None, {}),
        ("expired", "system", None, {}),
    ],
)
async def test_record_decisions_writes_an_audit_record_and_an_event_per_approval(
    databases: Databases,
    decision: DecidedApprovalStatus,
    actor: str,
    note: str | None,
    detail_note: dict[str, str | None],
) -> None:
    project_id = "prj_records"
    apr = await waiting_approval(databases, project_id)
    await set_permission_level(databases, project_id, "autonomous")
    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)

    await record_decisions(
        project_id,
        [apr],
        decision,
        actor=actor,
        databases=databases,
        audit_log=audit_log,
        event_log=event_log,
        note=note,
    )

    [row] = await _audit_rows(databases, project_id)
    assert (row["actor"], row["action"], row["decision"], row["target"]) == (
        actor,
        "approval",
        decision,
        "edit_file",
    )
    assert (row["risk_class"], row["permission_level"]) == ("write_code", "autonomous")
    [event] = await event_log.history(project_id, types=["approval.decided"])
    assert (event.source, event.subject_id) == (actor, apr.id)
    assert event.payload == {"approval_id": apr.id, "status": decision}
    assert set(row["detail"]) >= {"approval_id", "tool"}
    assert {k: v for k, v in row["detail"].items() if k == "note"} == detail_note


async def test_record_decisions_without_logs_writes_nothing(databases: Databases) -> None:
    project_id = "prj_nologs"
    apr = await waiting_approval(databases, project_id)

    await record_decisions(
        project_id,
        [apr],
        "expired",
        actor="system",
        databases=databases,
        audit_log=None,
        event_log=None,
    )

    assert await _audit_rows(databases, project_id) == []


async def test_a_users_decision_is_recorded_by_the_shared_function(
    databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlops_agents.agents.approvals import service as service_module

    calls = spy_on(service_module, monkeypatch)
    project_id = "prj_userdecision"
    apr = await waiting_approval(databases, project_id)
    service = ApprovalsService(
        databases,
        FakeSessionsForApproval(SessionStore(databases)),
        store=ApprovalStore(databases),
        audit_log=SqliteAuditLog(databases, secrets=[]),
        event_log=SqliteEventLog(databases),
    )

    await service.decide(project_id, apr.id, approve=False, note="Not now")

    assert calls == [(project_id, [apr.id], "rejected", "user", "Not now")]
    assert [r["decision"] for r in await _audit_rows(databases, project_id)] == ["rejected"]


async def test_stop_on_a_waiting_turn_records_its_cancels_by_the_shared_function(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mlops_agents.agents.runtime import waiting as waiting_module

    calls = spy_on(waiting_module, monkeypatch)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=StoreAskGate(databases),
        audit_log=SqliteAuditLog(databases, secrets=[]),
    )
    sub = env.manager.subscribe(env.project_id, "orchestrator", after_seq=None)
    await anext(sub)
    await env.manager.send(env.project_id, "orchestrator", "Run a command")
    await collect_until_turn_waiting(sub)
    [pending] = await ApprovalStore(databases).pending(env.project_id)

    await env.manager.stop(env.project_id, "orchestrator")

    assert calls == [(env.project_id, [pending.id], "cancelled", "user", None)]
    rows = await _audit_rows(databases, env.project_id)
    assert [(r["decision"], r["actor"]) for r in rows if r["action"] == "approval"] == [
        ("cancelled", "user")
    ]
    await env.manager.close()


async def test_recovery_records_its_expiries_by_the_shared_function(
    databases: Databases, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlops_agents.agents.runtime import store as store_module

    calls = spy_on(store_module, monkeypatch)
    project_id = "prj_recoveryrecords"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    apr = await waiting_approval(databases, project_id, turn_status="running")
    store = SessionStore(databases, audit_log=SqliteAuditLog(databases, secrets=[]))

    await store.recover(project_id, approval_store=ApprovalStore(databases))

    assert calls == [(project_id, [apr.id], "expired", "system", None)]
    assert [(r["decision"], r["actor"]) for r in await _audit_rows(databases, project_id)] == [
        ("expired", "system")
    ]
