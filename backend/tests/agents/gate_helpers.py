from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel
from sqlalchemy import insert

from mlops_agents.agents.runtime.hooks import GateCall
from mlops_agents.agents.tools.base import (
    AgentTool,
    FileAccess,
    Preview,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.clock import now_iso
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import sessions, turns


class DummyArgs(BaseModel):
    path: str | None = None
    command: str | None = None
    target: str | None = None


class FakeFileAccess(FileAccess):
    @property
    def root(self) -> Path:
        return Path("/workspace")

    async def tracked(self) -> list[str]:
        return []

    async def under(self, prefix: str) -> list[str]:
        return []

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return user_path or ""

    def invalidate(self) -> None:
        pass


class ReadTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "read_file"
    description: ClassVar[str] = "Dummy read tool"
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class WriteTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "edit_file"
    description: ClassVar[str] = "Dummy write tool"
    risk: ClassVar[RiskClass] = RiskClass.WRITE_CODE
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class RunTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "bash"
    description: ClassVar[str] = "Dummy run tool"
    risk: ClassVar[RiskClass] = RiskClass.RUN
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class GithubTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "open_pr"
    description: ClassVar[str] = "Dummy github tool"
    risk: ClassVar[RiskClass] = RiskClass.GITHUB
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class DeployTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "deploy_tool"
    description: ClassVar[str] = "Dummy deploy tool"
    risk: ClassVar[RiskClass] = RiskClass.DEPLOY
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class ProposeArgs(BaseModel):
    kind: object = None


class ProposeTool(AgentTool[ProposeArgs]):
    """A tool of class propose, as the proposals plan's Propose will be; its name is not read."""

    name: ClassVar[str] = "propose_something"
    description: ClassVar[str] = "Dummy propose tool"
    risk: ClassVar[RiskClass] = RiskClass.PROPOSE
    args_model: ClassVar[type[BaseModel]] = ProposeArgs

    async def run(self, ctx: ToolContext, args: ProposeArgs) -> ToolOutput:
        return ToolOutput("ok")


async def setup_session_and_turn(
    databases: Databases, project_id: str, session_id: str = "ses_1", turn_id: str = "trn_1"
) -> None:
    engine = await databases.project(project_id)
    now = now_iso()
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id=session_id,
                agent="orchestrator",
                summary=None,
                compacted_through=None,
                created_at=now,
            )
        )
        await conn.execute(
            insert(turns).values(
                id=turn_id,
                session_id=session_id,
                trigger_message_id="msg_1",
                status="running",
                error=None,
                created_at=now,
                started_at=now,
                ended_at=None,
            )
        )


def make_call(
    tool: AgentTool[Any],
    tool_call_id: str,
    project_id: str,
    turn_id: str = "trn_1",
    args: BaseModel | None = None,
) -> GateCall:
    ctx = ToolContext(
        project_id=project_id,
        agent="orchestrator",
        session_id="ses_1",
        turn_id=turn_id,
        files=FakeFileAccess(),
        task=None,
        tool_call_id=tool_call_id,
    )
    return GateCall(
        tool_call_id=tool_call_id,
        message_id="msg_1",
        tool=tool,
        args=args or DummyArgs(target="test_target"),
        ctx=ctx,
    )
