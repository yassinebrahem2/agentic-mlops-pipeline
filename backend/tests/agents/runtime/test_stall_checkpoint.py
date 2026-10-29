"""A stall checkpointed by an earlier build reads back with the fields added since."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from mlops_agents.agents.runtime.graph import turn_checkpointer
from mlops_agents.agents.runtime.stall import StallState
from mlops_agents.agents.tools.base import Preview


@dataclass(frozen=True)
class _StallBeforePrepare:
    """StallState as checkpointed before `node` existed: the same module and name, no `node`."""

    approval_id: str
    provider_id: str
    model: str
    step: int
    preview: Preview
    resolved: bool = False


_StallBeforePrepare.__module__ = StallState.__module__
_StallBeforePrepare.__name__ = StallState.__name__


async def test_a_stall_checkpointed_without_its_node_reads_back_as_a_model_stall(
    tmp_path: Path,
) -> None:
    preview = Preview(kind="text", title="fake: no usable key for m", body="Key 1: ok.")
    old = _StallBeforePrepare("apr_1", "prov_1", "m", 2, preview)
    async with aiosqlite.connect(tmp_path / "checkpoints.db") as conn:
        serde = turn_checkpointer(conn).serde

        read = serde.loads_typed(serde.dumps_typed(old))

    assert read == StallState("apr_1", "prov_1", "m", 2, preview, resolved=False, node="model")
