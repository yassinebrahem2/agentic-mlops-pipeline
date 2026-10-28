from __future__ import annotations

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.names import STATUS_BOARD_HOOK
from mlops_agents.agents.hooks.status_board import StatusBoardHook
from mlops_agents.events.contract import AgentStatus

CTX = HookContext(project_id="prj_1", agent="orchestrator", trigger_text="hi")


class _FakeBoard:
    def __init__(self, summary_text: str) -> None:
        self.summary_text = summary_text
        self.calls: list[str] = []

    async def snapshot(self, project_id: str) -> list[AgentStatus]:
        return []

    async def summary(self, project_id: str) -> str:
        self.calls.append(project_id)
        return self.summary_text


def test_the_hook_is_named_by_the_one_constant() -> None:
    assert StatusBoardHook().name == STATUS_BOARD_HOOK == "status_board"


async def test_renders_the_labelled_summary() -> None:
    board = _FakeBoard("- Orchestrator: idle\n- Retraining: running")
    hook = StatusBoardHook()
    hook.bind(board)
    assert await hook.render(CTX) == "Status board:\n- Orchestrator: idle\n- Retraining: running"
    assert board.calls == ["prj_1"]


async def test_an_empty_summary_renders_nothing() -> None:
    hook = StatusBoardHook()
    hook.bind(_FakeBoard(""))
    assert await hook.render(CTX) == ""


async def test_unbound_renders_its_own_line() -> None:
    assert await StatusBoardHook().render(CTX) == "[status_board: unavailable (not bound)]"
