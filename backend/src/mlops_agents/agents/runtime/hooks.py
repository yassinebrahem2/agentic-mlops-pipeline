"""The turn hooks the graph and the session manager call (I12). Protocols and dataclasses only."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.tools.base import AgentTool, Preview, ToolContext


@dataclass(frozen=True)
class GateCall:
    tool_call_id: str
    message_id: str  # the agent message the approval part goes into
    tool: AgentTool[Any]
    args: BaseModel
    ctx: ToolContext


@dataclass(frozen=True)
class GateDecision:
    outcome: Literal["allow", "deny", "ask", "approved", "rejected"]
    message: str | None  # the tool error for deny and rejected
    approval_id: str | None  # set for an ask
    preview: Preview | None  # set for an ask


class Gate(Protocol):
    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        """Never pauses."""
        ...

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        """Calls interrupt() while an approval is pending."""
        ...


class InboundSource(Protocol):
    async def take(self, project_id: str, session_id: str, turn_id: str) -> list[BaseMessage]: ...


class TurnEndCheck(Protocol):
    async def note(self, project_id: str, session_id: str, turn_id: str) -> str | None: ...


class TurnObserver(Protocol):
    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None: ...

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None: ...

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
    ) -> None: ...
