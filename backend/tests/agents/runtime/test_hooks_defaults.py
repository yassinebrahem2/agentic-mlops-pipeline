"""The stage 1 default of the inbound source (I12).

`FakeWorkspaces` is tested in test_agent.py, beside the workspace service it reads.
"""

from __future__ import annotations

from mlops_agents.agents.runtime.hooks import NoInbound


async def test_no_inbound_adds_nothing() -> None:
    assert await NoInbound().take("prj_1", "ses_1", "trn_1") == []
