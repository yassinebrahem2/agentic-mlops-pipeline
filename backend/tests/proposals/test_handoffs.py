"""Tests for SqliteHandoffs storage."""

from __future__ import annotations

import pytest

from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from tests.proposals.samples import handoff


@pytest.mark.asyncio
async def test_handoffs_store_and_current(databases: Databases) -> None:
    handoffs = SqliteHandoffs(databases)
    project_id = "prj_test"

    # Initially empty
    assert await handoffs.current(project_id) is None

    p1 = HandoffPayload.model_validate(handoff())
    v1 = await handoffs.store(project_id, "prp_1", p1)
    assert v1 >= 1

    current1 = await handoffs.current(project_id)
    assert current1 is not None
    assert current1.proposal_id == "prp_1"
    assert current1.version == v1
    assert current1.payload == p1

    # Idempotent: storing same proposal twice returns same version and adds no row
    v1_again = await handoffs.store(project_id, "prp_1", p1)
    assert v1_again == v1

    # Another proposal gets next version
    p2 = HandoffPayload.model_validate(handoff())
    v2 = await handoffs.store(project_id, "prp_2", p2)
    assert v2 > v1

    current2 = await handoffs.current(project_id)
    assert current2 is not None
    assert current2.proposal_id == "prp_2"
    assert current2.version == v2
