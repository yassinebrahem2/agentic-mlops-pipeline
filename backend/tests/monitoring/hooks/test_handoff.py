"""Tests for HandoffHooks: handoff registration, naming, update in place, disabling, and reconcile."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mlops_agents.credentials.project_secrets import hook_ref
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.handoff import HandoffHooks
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import HandoffPayload
from tests.proposals.samples import command, model
from tests.proposals.samples import handoff as sample_handoff


def _make_payload(
    project_commands: list[dict[str, Any]],
    model_commands: list[dict[str, Any]] | None = None,
    model_name: str = "cancel",
) -> HandoffPayload:
    models_data: list[dict[str, Any]] = []
    if model_commands is not None:
        models_data.append(model(name=model_name, commands=model_commands))
    raw = sample_handoff(
        commands=project_commands,
        models=models_data,
    )
    return HandoffPayload.model_validate(raw)


@pytest.mark.asyncio
async def test_stored_handoff_registers_hooks_and_numbers_duplicates(
    service: HooksService,
    handoffs: SqliteHandoffs,
    handoff_hooks: HandoffHooks,
) -> None:
    project_id = new_id("prj")
    # One project command (rollback) and one model command (train)
    cmd1 = command(kind="rollback", template="echo rollback 1")
    cmd2 = command(kind="train", template="echo train 1")
    payload = _make_payload(project_commands=[cmd1], model_commands=[cmd2])

    view = HandoffView(
        proposal_id="prp_1",
        version=1,
        approved=datetime.now(UTC),
        payload=payload,
    )

    await handoff_hooks.stored(project_id, view)

    hooks = await service.list(project_id)
    assert len(hooks) == 2

    project_hook = next(h for h in hooks if h.model is None)
    assert project_hook.name == "rollback"
    assert project_hook.status == "active"
    assert project_hook.credential_set is False
    assert project_hook.source == "handoff"
    assert project_hook.spec.template == "echo rollback 1"

    model_hook = next(h for h in hooks if h.model == "cancel")
    assert model_hook.name == "train"
    assert model_hook.status == "active"
    assert model_hook.credential_set is False
    assert model_hook.source == "handoff"
    assert model_hook.spec.template == "echo train 1"

    # Now test two commands of one kind: they get "-2"
    cmd3 = command(kind="rollback", template="echo rollback 2")
    payload2 = _make_payload(project_commands=[cmd1, cmd3], model_commands=[cmd2])
    view2 = HandoffView(
        proposal_id="prp_2",
        version=2,
        approved=datetime.now(UTC),
        payload=payload2,
    )
    await handoff_hooks.stored(project_id, view2)

    hooks2 = await service.list(project_id)
    assert len(hooks2) == 3
    hook_names = {(h.model, h.name) for h in hooks2}
    assert (None, "rollback") in hook_names
    assert (None, "rollback-2") in hook_names
    assert ("cancel", "train") in hook_names


@pytest.mark.asyncio
async def test_storing_again_and_reconcile_create_nothing_new(
    service: HooksService,
    handoffs: SqliteHandoffs,
    handoff_hooks: HandoffHooks,
) -> None:
    project_id = new_id("prj")
    cmd1 = command(kind="rollback", template="echo rollback 1")
    cmd2 = command(kind="train", template="echo train 1")
    payload = _make_payload(project_commands=[cmd1], model_commands=[cmd2])

    # Store via handoffs directly, with observer registered
    handoffs.add_observer(handoff_hooks)
    await handoffs.store(project_id, "prp_1", payload)

    hooks_before = await service.list(project_id)
    assert len(hooks_before) == 2
    ids_before = {h.id for h in hooks_before}

    # Store again: same version/proposal or calling stored directly creates nothing new
    current = await handoffs.current(project_id)
    assert current is not None
    await handoff_hooks.stored(project_id, current)

    hooks_after_store = await service.list(project_id)
    assert {h.id for h in hooks_after_store} == ids_before

    # Startup reconcile creates nothing new
    await handoff_hooks.reconcile([project_id])
    hooks_after_reconcile = await service.list(project_id)
    assert {h.id for h in hooks_after_reconcile} == ids_before


@pytest.mark.asyncio
async def test_reconcile_registers_hooks_stored_before_observer_existed(
    service: HooksService,
    handoffs: SqliteHandoffs,
    handoff_hooks: HandoffHooks,
) -> None:
    project_id = new_id("prj")
    cmd1 = command(kind="rerun", template="echo rerun")
    payload = _make_payload(project_commands=[cmd1], model_commands=[])

    # Store BEFORE adding observer
    await handoffs.store(project_id, "prp_pre", payload)

    # Hooks not yet registered
    hooks_before = await service.list(project_id)
    assert len(hooks_before) == 0

    # Reconcile registers them
    await handoff_hooks.reconcile([project_id])
    hooks_after = await service.list(project_id)
    assert len(hooks_after) == 1
    assert hooks_after[0].name == "rerun"
    assert hooks_after[0].source == "handoff"


@pytest.mark.asyncio
async def test_update_in_place_and_disable_dropped_hooks(
    service: HooksService,
    secrets: Any,
    handoff_hooks: HandoffHooks,
) -> None:
    project_id = new_id("prj")
    cmd_rb = command(kind="rollback", template="echo rb v1")
    cmd_tr = command(kind="train", template="echo tr v1")
    payload1 = _make_payload(project_commands=[cmd_rb], model_commands=[cmd_tr])

    view1 = HandoffView(
        proposal_id="prp_1",
        version=1,
        approved=datetime.now(UTC),
        payload=payload1,
    )
    await handoff_hooks.stored(project_id, view1)

    hooks1 = await service.list(project_id)
    rb_hook = next(h for h in hooks1 if h.name == "rollback")
    tr_hook = next(h for h in hooks1 if h.name == "train")

    # Set a credential for rb_hook and suspend tr_hook to verify they are kept
    await secrets.put(hook_ref(project_id, rb_hook.id), "rb_secret_val")
    await service.suspend(project_id, tr_hook.id, "temporary hold")

    # Verify state before update
    rb_before = await service.get(project_id, rb_hook.id)
    assert rb_before is not None and rb_before.credential_set is True
    tr_before = await service.get(project_id, tr_hook.id)
    assert tr_before is not None and tr_before.status == "suspended"

    # Newer handoff:
    # 1. rb_hook updates template ("echo rb v2")
    # 2. tr_hook is DROPPED from newer handoff
    cmd_rb_v2 = command(kind="rollback", template="echo rb v2")
    payload2 = _make_payload(project_commands=[cmd_rb_v2], model_commands=[])
    view2 = HandoffView(
        proposal_id="prp_2",
        version=2,
        approved=datetime.now(UTC),
        payload=payload2,
    )
    await handoff_hooks.stored(project_id, view2)

    # Check rb_hook: updated in place (same ID, status active, credential kept)
    rb_after = await service.get(project_id, rb_hook.id)
    assert rb_after is not None
    assert rb_after.id == rb_hook.id
    assert rb_after.spec.template == "echo rb v2"
    assert rb_after.status == "active"
    assert rb_after.credential_set is True

    # Check tr_hook: was dropped, so it must be disabled
    tr_after = await service.get(project_id, tr_hook.id)
    assert tr_after is not None
    assert tr_after.id == tr_hook.id
    assert tr_after.status == "disabled"


@pytest.mark.asyncio
async def test_reconcile_logs_failure_and_continues(
    handoff_hooks: HandoffHooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    p1 = new_id("prj")
    p_fail = new_id("prj")
    p2 = new_id("prj")

    async def fake_stored(project_id: str, handoff: HandoffView) -> None:
        calls.append(project_id)
        if project_id == p_fail:
            raise RuntimeError("Database locked")

    monkeypatch.setattr(handoff_hooks, "stored", fake_stored)

    # Mock handoffs.current to return non-None for each project
    fake_handoff = HandoffView(
        proposal_id="prp_1",
        version=1,
        approved=datetime.now(UTC),
        payload=_make_payload([]),
    )
    handoff_hooks._handoffs.current = AsyncMock(return_value=fake_handoff)  # type: ignore

    await handoff_hooks.reconcile([p1, p_fail, p2])
    assert calls == [p1, p_fail, p2]
