"""The turn hooks the graph and the session manager call (I12), and their stage 1 defaults.

Each default keeps stage 1's behavior until the plan that wires the real hook deletes it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.tools.base import AgentTool, FileAccess, Preview, TaskRef, ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.workspace.service import WorkspaceService


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


class AllowAllGate:
    """Stage 1's gate: every call runs. The approvals plan replaces it."""

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        allow = GateDecision(outcome="allow", message=None, approval_id=None, preview=None)
        return {call.tool_call_id: allow for call in calls}

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        return decisions


class NoInbound:
    """Stage 1: nothing reaches a running turn. The messaging plan replaces it."""

    async def take(self, project_id: str, session_id: str, turn_id: str) -> list[BaseMessage]:
        return []


class NoTurnEndCheck:
    """Stage 1: a turn ends when the model ends it. The github-work plan replaces it."""

    async def note(self, project_id: str, session_id: str, turn_id: str) -> str | None:
        return None


class RepositoryOnlyWorkspaces:
    """Stage 1: no task, the repository's files. The tasks plan replaces it."""

    def __init__(self, *, workspace: WorkspaceService, git: GitRunner) -> None:
        self._workspace = workspace
        self._git = git

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        return None

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        if task is not None:
            raise ValueError(f"No task workspaces here, so no files for task {task.id}.")
        project = await self._workspace.get(project_id)
        return RepoFiles(self._workspace.repo_root(project), self._git)


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
