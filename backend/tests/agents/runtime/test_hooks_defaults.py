"""The stage 1 defaults of the inbound source and the turn-end check (I12).

`FakeWorkspaces` is tested in test_agent.py, beside the workspace service it reads.
"""

from __future__ import annotations

from mlops_agents.agents.runtime.hooks import (
    NoInbound,
    NoTurnEndCheck,
)


async def test_no_inbound_and_no_turn_end_check_add_nothing() -> None:
    assert await NoInbound().take("prj_1", "ses_1", "trn_1") == []
    assert await NoTurnEndCheck().note("prj_1", "ses_1", "trn_1") is None
