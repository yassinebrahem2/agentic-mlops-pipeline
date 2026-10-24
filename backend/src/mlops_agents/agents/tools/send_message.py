from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.messaging.contract import MessageLimitReached
from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.errors import DomainError

if TYPE_CHECKING:
    from mlops_agents.agents.messaging.bus import SqliteMessageBus


class SendMessageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(description="Name of the agent to message, as ListAgents shows it.")
    message: str = Field(
        min_length=1, description="What the agent needs to know or do. It does not see your chat."
    )


class SendMessageTool(AgentTool[SendMessageArgs]):
    name: ClassVar[str] = "SendMessage"
    description: ClassVar[str] = (
        "Send a message to another agent. It reaches that agent's session and you do not wait "
        "for an answer: a reply arrives later as a message from that agent."
    )
    risk: ClassVar[RiskClass] = RiskClass.MESSAGE
    args_model: ClassVar[type[BaseModel]] = SendMessageArgs

    def __init__(self, bus: SqliteMessageBus) -> None:
        self._bus = bus

    async def run(self, ctx: ToolContext, args: SendMessageArgs) -> ToolOutput:
        try:
            await self._bus.send(
                ctx.project_id,
                sender=ctx.agent,
                sender_turn_id=ctx.turn_id,
                to=args.to,
                text=args.message,
            )
        except (DomainError, MessageLimitReached) as exc:
            raise ToolFailure(str(exc)) from None
        return ToolOutput(f"Sent to {self._bus.display_name(args.to)}.")
