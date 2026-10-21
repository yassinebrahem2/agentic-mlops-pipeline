import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from mlops_agents.agents.runtime.contract import (
    AgentSessions,
    MessageView,
    Part,
    SessionView,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

VALID_EVENT_TYPES: list[str] = [
    "message.created",
    "text.delta",
    "tool.started",
    "tool.finished",
    "message.updated",
    "turn.ended",
    "resync",
]


def test_part_parses_text_from_json() -> None:
    raw = json.dumps({"type": "text", "text": "hello world"})
    adapter = TypeAdapter(Part)
    part = adapter.validate_json(raw)
    assert isinstance(part, TextPart)
    assert part.type == "text"
    assert part.text == "hello world"


def test_part_parses_tool_call_from_json() -> None:
    raw = json.dumps(
        {
            "type": "tool_call",
            "id": "call_123",
            "name": "Read",
            "input": {"path": "README.md"},
        }
    )
    adapter = TypeAdapter(Part)
    part = adapter.validate_json(raw)
    assert isinstance(part, ToolCallPart)
    assert part.type == "tool_call"
    assert part.id == "call_123"
    assert part.name == "Read"
    assert part.input == {"path": "README.md"}


def test_part_parses_tool_result_from_json() -> None:
    raw = json.dumps(
        {
            "type": "tool_result",
            "tool_call_id": "call_123",
            "content": "File contents",
            "is_error": False,
        }
    )
    adapter = TypeAdapter(Part)
    part = adapter.validate_json(raw)
    assert isinstance(part, ToolResultPart)
    assert part.type == "tool_result"
    assert part.tool_call_id == "call_123"
    assert part.content == "File contents"
    assert part.is_error is False


def test_part_rejects_unknown_type() -> None:
    raw = json.dumps({"type": "unknown", "text": "hello"})
    adapter = TypeAdapter(Part)
    with pytest.raises(ValidationError):
        adapter.validate_json(raw)


def test_models_reject_missing_required_fields() -> None:
    # TextPart requires type
    with pytest.raises(ValidationError):
        TextPart.model_validate({"text": "hello"})

    # ToolCallPart requires type
    with pytest.raises(ValidationError):
        ToolCallPart.model_validate({"id": "c1", "name": "Read", "input": {}})

    # ToolResultPart requires type and is_error
    with pytest.raises(ValidationError):
        ToolResultPart.model_validate({"tool_call_id": "c1", "content": "res", "is_error": False})
    with pytest.raises(ValidationError):
        ToolResultPart.model_validate(
            {"type": "tool_result", "tool_call_id": "c1", "content": "res"}
        )

    # MessageView requires turn_id and from_agent (even if None)
    base_msg: dict[str, Any] = {
        "id": "msg_01",
        "author_kind": "user",
        "author_name": "user",
        "parts": [{"type": "text", "text": "hi"}],
        "status": "complete",
        "created_at": "2026-10-21T18:00:00Z",
    }
    with pytest.raises(ValidationError):
        MessageView.model_validate(base_msg)  # missing turn_id and from_agent
    with pytest.raises(ValidationError):
        MessageView.model_validate({**base_msg, "turn_id": None})  # missing from_agent
    with pytest.raises(ValidationError):
        MessageView.model_validate({**base_msg, "from_agent": None})  # missing turn_id

    # Valid with explicit None
    valid_msg = MessageView.model_validate({**base_msg, "turn_id": None, "from_agent": None})
    assert valid_msg.turn_id is None
    assert valid_msg.from_agent is None

    # StreamEvent requires data
    with pytest.raises(ValidationError):
        StreamEvent.model_validate({"seq": 1, "type": "resync"})


def test_message_view_round_trip() -> None:
    message = MessageView(
        id="msg_01",
        turn_id="turn_01",
        author_kind="agent",
        author_name="orchestrator",
        from_agent=None,
        parts=[
            TextPart(type="text", text="Starting tool execution"),
            ToolCallPart(
                type="tool_call",
                id="call_1",
                name="Read",
                input={"path": "src/main.py"},
            ),
            ToolResultPart(
                type="tool_result",
                tool_call_id="call_1",
                content="def main(): pass",
                is_error=False,
            ),
        ],
        status="complete",
        created_at="2026-10-21T18:00:00Z",
    )
    dumped = message.model_dump_json()
    restored = MessageView.model_validate_json(dumped)
    assert restored == message
    assert restored.parts[0].type == "text"
    assert isinstance(restored.parts[1], ToolCallPart)
    assert isinstance(restored.parts[2], ToolResultPart)


def test_session_view_round_trip() -> None:
    message = MessageView(
        id="msg_01",
        turn_id=None,
        author_kind="user",
        author_name="user",
        from_agent=None,
        parts=[TextPart(type="text", text="Hello")],
        status="complete",
        created_at="2026-10-21T17:59:00Z",
    )
    session = SessionView(
        id="ses_01",
        agent="orchestrator",
        busy=False,
        messages=[message],
        last_seq=10,
    )
    dumped = session.model_dump_json()
    restored = SessionView.model_validate_json(dumped)
    assert restored == session
    assert len(restored.messages) == 1
    assert restored.messages[0].id == "msg_01"


@pytest.mark.parametrize("event_type", VALID_EVENT_TYPES)
def test_stream_event_accepts_valid_types(event_type: Any) -> None:
    event = StreamEvent(seq=1, type=event_type, data={"key": "val"})
    assert event.type == event_type
    assert event.seq == 1
    assert event.data == {"key": "val"}


def test_stream_event_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        StreamEvent.model_validate({"seq": 1, "type": "invalid.type", "data": {}})


class DummySessions:
    async def get(self, project_id: str, agent: str) -> SessionView:
        return SessionView(
            id="ses_dummy",
            agent=agent,
            busy=False,
            messages=[],
            last_seq=0,
        )

    async def send(self, project_id: str, agent: str, text: str) -> MessageView:
        return MessageView(
            id="msg_dummy",
            turn_id=None,
            author_kind="user",
            author_name="user",
            from_agent=None,
            parts=[TextPart(type="text", text=text)],
            status="complete",
            created_at="2026-10-21T18:00:00Z",
        )

    async def stop(self, project_id: str, agent: str) -> None:
        pass

    async def _dummy_stream(self) -> AsyncIterator[StreamEvent]:
        if False:
            yield StreamEvent(seq=1, type="resync", data={})

    def subscribe(
        self, project_id: str, agent: str, after_seq: int | None
    ) -> AsyncIterator[StreamEvent]:
        return self._dummy_stream()

    async def close_project(self, project_id: str) -> None:
        """Before a project is removed: ends its running turns (interrupted), drops its hubs and
        subscribers, closes its database handles; later calls for that project start fresh."""
        pass


def test_agent_sessions_protocol() -> None:
    sessions: AgentSessions = DummySessions()
    assert sessions is not None
