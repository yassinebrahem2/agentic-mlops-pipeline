from __future__ import annotations

from mlops_agents.agents.tools.base import FileAccess, TaskRef, ToolContext


def tool_context(
    files: FileAccess,
    *,
    project_id: str = "prj_test",
    agent: str = "tester",
    session_id: str = "ses_test",
    turn_id: str = "trn_test",
    task: TaskRef | None = None,
    tool_call_id: str = "call_test",
) -> ToolContext:
    """A ToolContext for tests that exercise a tool against a given file view."""
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id=session_id,
        turn_id=turn_id,
        files=files,
        task=task,
        tool_call_id=tool_call_id,
    )
