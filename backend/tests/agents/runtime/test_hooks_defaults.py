"""The stage 1 defaults of the gate, the inbound source and the turn-end check (I12).

`RepositoryOnlyWorkspaces` is tested in test_agent.py, beside the workspace service it reads.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from mlops_agents.agents.runtime.hooks import (
    AllowAllGate,
    GateCall,
    GateDecision,
    NoInbound,
    NoTurnEndCheck,
)
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from tests.agents.ctx import tool_context


class _Args(BaseModel):
    file_path: str


async def test_allow_all_gate_allows_every_call_and_waits_for_nothing(tmp_path: Path) -> None:
    read = default_registry().get("Read")
    assert read is not None
    ctx = tool_context(RepoFiles(tmp_path, GitRunner(tmp_path / "bin")))
    calls = [
        GateCall(
            tool_call_id=cid, message_id="msg_1", tool=read, args=_Args(file_path="a"), ctx=ctx
        )
        for cid in ("c1", "c2")
    ]
    gate = AllowAllGate()

    decisions = await gate.decide(calls)

    allow = GateDecision(outcome="allow", message=None, approval_id=None, preview=None)
    assert decisions == {"c1": allow, "c2": allow}
    assert await gate.wait("prj_1", "trn_1", decisions) == decisions


async def test_no_inbound_and_no_turn_end_check_add_nothing() -> None:
    assert await NoInbound().take("prj_1", "ses_1", "trn_1") == []
    assert await NoTurnEndCheck().note("prj_1", "ses_1", "trn_1") is None
