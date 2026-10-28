from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import BaseMessage
from langgraph.types import Command

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.runtime.context import build_messages, top_level_layout
from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools.base import AgentTool, FileAccess, ToolRegistry
from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.errors import DomainError
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.workspace.models import Project
from mlops_agents.workspace.service import WorkspaceService


@dataclass(frozen=True)
class Agent:
    definition: AgentDefinition
    project: Project
    layout: str  # top_level_layout of the repository
    model: ResolvedModel
    max_steps: int | None
    tools: tuple[AgentTool[Any], ...]  # exactly the definition's tools, in its order
    files: FileAccess  # the repository's view the layout was read from; the turn's tools reuse it
    hook_context: str | None = None  # the context hooks' blocks, rendered for this turn


class TurnRefused(Exception):
    def __init__(self, end: Literal["done", "failed"], text: str) -> None:
        super().__init__(text)
        self.end: Literal["done", "failed"] = end
        self.text: str = text


class AgentBuilder:
    def __init__(
        self,
        *,
        catalog: AgentCatalog,
        tools: ToolRegistry,
        settings: AgentSettingsService,
        workspace: WorkspaceService,
        workspaces: TaskWorkspaces,
        store: SessionStore,
        hooks: HookRegistry | None = None,
    ) -> None:
        self._catalog = catalog
        self._tools = tools
        self._settings = settings
        self._workspace = workspace
        self._workspaces = workspaces
        self._store = store
        self._hooks = hooks

    async def build(
        self,
        project_id: str,
        agent: str,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> Agent:
        """The agent for one turn. Its hooks run only for a turn (both IDs given)."""
        project = await self._workspace.get(project_id)
        if project.status in ("missing", "clone_failed"):
            detail = project.status_detail or f"Project is {project.status}."
            raise TurnRefused("failed", detail)
        if project.status == "empty":
            raise TurnRefused(
                "done",
                "This repository has no committed files yet. Commit your files, then ask again.",
            )

        try:
            resolved_model, max_steps = await self._settings.resolve(project_id, agent)
        except DomainError as exc:
            raise TurnRefused("failed", exc.message) from exc
        except KeyStoreError as exc:
            raise TurnRefused("failed", str(exc)) from exc

        definition = self._catalog.get(agent)
        files = await self._workspaces.files(project_id, None)
        layout = await top_level_layout(files)

        agent_tools: list[AgentTool[Any]] = []
        for tool_name in definition.tools:
            t = self._tools.get(tool_name)
            if t is not None:
                agent_tools.append(t)

        hook_context: str | None = None
        if (
            session_id is not None
            and turn_id is not None
            and self._hooks is not None
            and definition.hook_names
        ):
            ctx = HookContext(
                project_id=project_id,
                agent=definition.name,
                trigger_text=await self._trigger_text(project_id, session_id, turn_id),
            )
            hook_context = await self._hooks.run(definition.hook_names, ctx)

        return Agent(
            definition=definition,
            project=project,
            layout=layout,
            model=resolved_model,
            max_steps=max_steps,
            tools=tuple(agent_tools),
            files=files,
            hook_context=hook_context,
        )

    async def _trigger_text(self, project_id: str, session_id: str, turn_id: str) -> str:
        """The text of the message that started the turn: a user message, a handoff message or
        a wake-up, all stored alike."""
        trigger_id = await self._store.turn_trigger_id(project_id, turn_id)
        if trigger_id is None:
            return ""
        trigger = await self._store.message(project_id, session_id, trigger_id)
        if trigger is None:
            return ""
        return "".join(p.text for p in trigger.parts if isinstance(p, TextPart))


async def resume_command(
    agent: Agent, summary: str | None, db_path: Path, turn_id: str
) -> Command[Any]:
    """The command that resumes a waiting turn with this build's hook text.

    A resumed graph's messages come from its checkpoint, so the fresh system message takes the
    checkpointed first message's id: `add_messages` then replaces it in place, and the turn
    never holds two system messages.
    """
    # waiting imports the turn graph, which imports Agent from this module.
    from mlops_agents.agents.runtime.waiting import open_checkpointer

    async with open_checkpointer(db_path, turn_id) as holder:
        # First, before any read: leaving the context deletes the thread otherwise, which would
        # destroy the waiting turn, also when the read raises.
        holder.preserved = True
        checkpoint = await holder.saver.aget_tuple({"configurable": {"thread_id": turn_id}})
    checkpointed: list[BaseMessage] = (
        [] if checkpoint is None else checkpoint.checkpoint["channel_values"].get("messages", [])
    )
    if not checkpointed:
        return Command(resume=True)
    system = build_messages(
        agent.definition,
        agent.project,
        agent.layout,
        summary,
        [],
        hook_context=agent.hook_context,
    )[0]
    system.id = checkpointed[0].id
    return Command(resume=True, update={"messages": [system]})
