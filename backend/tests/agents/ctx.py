from mlops_agents.agents.tools.base import FileAccess, ToolContext


def tool_context(files: FileAccess) -> ToolContext:
    """A ToolContext for tests that exercise a tool against a given file view."""
    return ToolContext(
        project_id="prj_test",
        agent="tester",
        session_id="ses_test",
        turn_id="trn_test",
        files=files,
        task=None,
    )
