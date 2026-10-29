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


@pytest.mark.asyncio
async def test_handoff_observers_seam(databases: Databases) -> None:
    from mlops_agents.proposals.contract import HandoffView

    handoffs = SqliteHandoffs(databases)
    project_id = "prj_obs"

    observed_1: list[HandoffView] = []
    observed_2: list[HandoffView] = []

    class FailingObserver:
        async def stored(self, project_id: str, handoff: HandoffView) -> None:
            raise RuntimeError("observer crash")

    class RecordingObserver:
        def __init__(self, target_list: list[HandoffView]) -> None:
            self._list = target_list

        async def stored(self, project_id: str, handoff: HandoffView) -> None:
            self._list.append(handoff)

    failing = FailingObserver()
    rec1 = RecordingObserver(observed_1)
    rec2 = RecordingObserver(observed_2)

    handoffs.add_observer(rec1)
    handoffs.add_observer(failing)
    handoffs.add_observer(rec2)

    # 1. Store proposal 1
    p1 = HandoffPayload.model_validate(handoff())
    v1 = await handoffs.store(project_id, "prp_obs1", p1)
    assert v1 >= 1

    # Both rec1 and rec2 were called despite failing observer raising
    assert len(observed_1) == 1
    assert len(observed_2) == 1
    assert observed_1[0].proposal_id == "prp_obs1"
    assert observed_2[0].proposal_id == "prp_obs1"

    # 2. Store proposal 2 (newer)
    p2 = HandoffPayload.model_validate(handoff())
    v2 = await handoffs.store(project_id, "prp_obs2", p2)
    assert v2 > v1
    assert len(observed_1) == 2
    assert observed_1[1].proposal_id == "prp_obs2"

    # 3. Store older proposal 1 again (idempotent replay)
    # Storing an older proposal again after a newer one delivers current() (the newer handoff: v2 / prp_obs2)
    v1_repeat = await handoffs.store(project_id, "prp_obs1", p1)
    assert v1_repeat == v1
    assert len(observed_1) == 3
    assert observed_1[2].proposal_id == "prp_obs2"
    assert observed_1[2].version == v2
