from __future__ import annotations

from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from mlops_agents.agents.catalog import AgentDefinition
from mlops_agents.agents.runtime.contract import (
    MessageStatus,
    MessageView,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.tools.base import FileAccess
from mlops_agents.workspace.models import Project


async def top_level_layout(files: FileAccess) -> str:
    """Return sorted unique top-level files and directories (with trailing /), capped at 100."""
    tracked = await files.tracked()
    top_levels: set[str] = set()
    for path in tracked:
        p = path.replace("\\", "/")
        if "/" in p:
            top_levels.add(p.split("/", 1)[0] + "/")
        else:
            top_levels.add(p)

    sorted_entries = sorted(top_levels)
    if len(sorted_entries) > 100:
        remaining = len(sorted_entries) - 100
        return "\n".join([*sorted_entries[:100], f"(and {remaining} more)"])
    return "\n".join(sorted_entries)


def build_messages(
    definition: AgentDefinition,
    project: Project,
    layout: str,
    summary: str | None,
    history: list[MessageView],
    hook_context: str | None = None,
) -> list[BaseMessage]:
    """Assemble the langchain BaseMessage list for an agent run."""
    # 1. System message
    sections = [definition.prompt.strip()]

    project_lines = [
        "Project:",
        f"Name: {project.name}",
        f"Source: {project.source}",
    ]
    if project.default_branch:
        project_lines.append(f"Default branch: {project.default_branch}")
    if layout:
        project_lines.extend(["Top-level layout:", layout])
    sections.append("\n".join(project_lines))

    if summary:
        sections.append(f"Summary of the earlier conversation:\n{summary}")

    # The context hooks' blocks, already wrapped as data by the registry.
    if hook_context:
        sections.append(hook_context)

    system_content = "\n\n".join(sections)
    messages: list[BaseMessage] = [SystemMessage(content=system_content)]

    # 2. History
    for message in history:
        if message.status == "queued" or message.author_kind == "system":
            continue

        if message.author_kind == "user":
            user_text = "".join(p.text for p in message.parts if isinstance(p, TextPart))
            if message.from_agent:
                user_text = f"Message from {message.from_agent}:\n{user_text}"
            messages.append(HumanMessage(content=user_text))

        elif message.author_kind == "agent":
            current_text: list[str] = []
            current_tool_calls: list[dict[str, Any]] = []
            unresolved_calls: list[str] = []

            def flush_ai() -> None:
                nonlocal current_text, current_tool_calls, unresolved_calls
                if current_text or current_tool_calls:
                    messages.append(
                        AIMessage(
                            content="".join(current_text),
                            tool_calls=current_tool_calls,
                        )
                    )
                    unresolved_calls = [tc["id"] for tc in current_tool_calls]
                    current_text = []
                    current_tool_calls = []

            def resolve_synthetic(status: MessageStatus) -> None:
                nonlocal unresolved_calls
                synthetic_content = (
                    "cancelled by the user" if status == "stopped" else "interrupted"
                )
                for call_id in unresolved_calls:
                    messages.append(
                        ToolMessage(
                            tool_call_id=call_id,
                            content=synthetic_content,
                        )
                    )
                unresolved_calls = []

            for part in message.parts:
                if isinstance(part, TextPart):
                    if unresolved_calls:
                        resolve_synthetic(message.status)
                    current_text.append(part.text)
                elif isinstance(part, ToolCallPart):
                    if unresolved_calls:
                        resolve_synthetic(message.status)
                    current_tool_calls.append(
                        {"id": part.id, "name": part.name, "args": part.input}
                    )
                elif isinstance(part, ToolResultPart):
                    flush_ai()
                    if part.tool_call_id in unresolved_calls:
                        unresolved_calls.remove(part.tool_call_id)
                    messages.append(
                        ToolMessage(
                            tool_call_id=part.tool_call_id,
                            content=part.content,
                        )
                    )

            flush_ai()
            if unresolved_calls:
                resolve_synthetic(message.status)

    return messages
