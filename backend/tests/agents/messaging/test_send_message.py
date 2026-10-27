from __future__ import annotations

from typing import cast

from mlops_agents.agents.messaging.settings import MessagingLimits
from mlops_agents.agents.tools.base import FileAccess, RiskClass, ToolContext
from mlops_agents.agents.tools.send_message import SendMessageTool
from tests.agents.messaging.bus_env import BusEnv
from tests.agents.runtime.session_env import collect_until_turn_ended


def _ctx(env: BusEnv, agent: str, turn: str = "trn_none") -> ToolContext:
    return ToolContext(
        project_id=env.session.project_id,
        agent=agent,
        session_id="ses_test",
        turn_id=turn,
        files=cast(FileAccess, object()),  # the tool reads no files
        task=None,
    )


def _tool(env: BusEnv) -> SendMessageTool:
    return SendMessageTool(env.bus)


async def test_the_tool_answers_with_the_recipients_display_name(bus_env: BusEnv) -> None:
    manager = bus_env.session.manager
    project_id = bus_env.session.project_id
    view = await manager.get(project_id, "beta")
    sub = manager.subscribe(project_id, "beta", after_seq=view.last_seq)

    out = await _tool(bus_env).invoke(_ctx(bus_env, "alpha"), {"to": "beta", "message": "Hello"})
    await collect_until_turn_ended(sub)

    assert (out.content, out.is_error) == ("Sent to Beta.", False)
    assert SendMessageTool.risk is RiskClass.MESSAGE


async def test_a_forbidden_or_unknown_recipient_is_a_tool_error(bus_env: BusEnv) -> None:
    for to in ("gamma", "nobody"):
        out = await _tool(bus_env).invoke(_ctx(bus_env, "alpha"), {"to": to, "message": "Hi"})
        assert out.is_error


async def test_a_limit_is_a_tool_error_telling_the_agent_to_report(bus_env: BusEnv) -> None:
    await bus_env.settings.set(
        bus_env.session.project_id, MessagingLimits(max_hops=1, max_tokens=9)
    )
    manager = bus_env.session.manager
    project_id = bus_env.session.project_id
    view = await manager.get(project_id, "beta")
    sub = manager.subscribe(project_id, "beta", after_seq=view.last_seq)
    await _tool(bus_env).invoke(_ctx(bus_env, "alpha"), {"to": "beta", "message": "Hello"})
    events = await collect_until_turn_ended(sub)
    beta_turn = events[-1].data.turn_id

    out = await _tool(bus_env).invoke(
        _ctx(bus_env, "beta", beta_turn), {"to": "alpha", "message": "Reply"}
    )

    assert out.is_error
    assert "report to the user" in out.content


async def test_an_empty_message_is_an_argument_error(bus_env: BusEnv) -> None:
    out = await _tool(bus_env).invoke(_ctx(bus_env, "alpha"), {"to": "beta", "message": ""})

    assert out.is_error
