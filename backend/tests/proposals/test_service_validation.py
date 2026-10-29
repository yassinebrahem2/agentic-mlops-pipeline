"""Tests for proposal payload validation, secret field absence, and duplicate draft rejection."""

from __future__ import annotations

from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import select

from mlops_agents.errors import DomainError
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.payloads import ConnectionsPayload, HandoffPayload
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import proposals as proposals_table
from tests.proposals.conftest import ScriptedTestKind
from tests.proposals.samples import connection


def test_payload_models_have_no_secret_fields() -> None:
    visited: set[type[BaseModel]] = set()

    def check_type(typ: Any) -> None:
        if typ is SecretStr:
            raise AssertionError("Found SecretStr in payload model field annotations")
        origin = get_origin(typ)
        if origin is not None:
            for arg in get_args(typ):
                check_type(arg)
        elif isinstance(typ, type) and issubclass(typ, BaseModel):
            check_model(typ)

    def check_model(model_cls: type[BaseModel]) -> None:
        if model_cls in visited:
            return
        visited.add(model_cls)
        for field_name, field_info in model_cls.model_fields.items():
            assert field_info.annotation is not SecretStr, f"{model_cls}.{field_name} is SecretStr"
            lower = field_name.lower()
            assert "secret" not in lower, f"{model_cls} has secret field {field_name}"
            assert "password" not in lower, f"{model_cls} has password field {field_name}"
            check_type(field_info.annotation)

    check_model(ConnectionsPayload)
    check_model(HandoffPayload)
    # Ensure recursion traversed through nested models in list, union, dict
    assert len(visited) > 10, f"Expected deep recursion into models, visited only {len(visited)}"


@pytest.mark.asyncio
async def test_create_invalid_payload_raises_422_and_stores_nothing(
    service: ProposalsService,
    kinds: ProposalKinds,
    databases: Databases,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_val"
    sensitive_value = "my_super_secret_payload_input"

    with pytest.raises(DomainError) as exc_info:
        await service.create(
            project_id,
            "architect",
            test_kind.name,
            "key1",
            {"connections": [{"name": sensitive_value, "invalid_field": 123}]},
            "trn_1",
            "call_1",
        )
    assert exc_info.value.status == 422
    assert exc_info.value.code == "proposal_invalid_payload"
    # Error message summarizes fields without leaking submitted input values
    assert sensitive_value not in exc_info.value.message

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(proposals_table))).all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_create_duplicate_in_same_turn_raises_and_stores_nothing(
    service: ProposalsService,
    kinds: ProposalKinds,
    databases: Databases,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_dup"
    valid_payload = {"connections": [connection()]}

    await service.create(
        project_id, "architect", test_kind.name, "key1", valid_payload, "trn_1", "call_1"
    )

    with pytest.raises(DomainError) as exc_info:
        await service.create(
            project_id, "architect", test_kind.name, "key1", valid_payload, "trn_1", "call_2"
        )
    assert exc_info.value.code == "proposal_duplicate_in_step"

    # Only call_1 was stored, nothing second
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(proposals_table).where(proposals_table.c.turn_id == "trn_1")
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["tool_call_id"] == "call_1"
