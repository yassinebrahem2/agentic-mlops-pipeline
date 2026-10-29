import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import TypeAdapter, ValidationError

from mlops_agents.agents.runtime.contract import (
    AgentSessions,
    ApprovalPart,
    MessageView,
    Part,
    Resync,
    ResyncData,
    SessionView,
    StreamEvent,
    TaskView,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    TurnWaiting,
)
from mlops_agents.agents.tools.base import Preview

MESSAGE: dict[str, Any] = {
    "id": "msg_01",
    "turn_id": None,
    "author_kind": "user",
    "author_name": "user",
    "from_agent": None,
    "parts": [{"type": "text", "text": "hi"}],
    "status": "complete",
    "created_at": "2026-10-21T18:00:00Z",
}

# One valid wire payload per event type: the shapes I1 fixes.
EVENT_DATA: dict[str, dict[str, Any]] = {
    "message.created": {"message": MESSAGE},
    "text.delta": {"message_id": "msg_01", "text": "Hel"},
    "tool.started": {"message_id": "msg_01", "tool_call_id": "c1", "name": "Read", "input": {}},
    "tool.finished": {
        "message_id": "msg_01",
        "tool_call_id": "c1",
        "content": "ok",
        "is_error": False,
    },
    "message.updated": {"message": MESSAGE},
    "turn.waiting": {"turn_id": "trn_01"},
    "turn.ended": {"turn_id": "trn_01", "status": "done", "error": None},
    "resync": {},
}
STREAM_EVENT: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)


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

    # Every event requires data
    with pytest.raises(ValidationError):
        STREAM_EVENT.validate_python({"seq": 1, "type": "resync"})


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
        current_task=None,
    )
    dumped = session.model_dump_json()
    restored = SessionView.model_validate_json(dumped)
    assert restored == session
    assert len(restored.messages) == 1
    assert restored.messages[0].id == "msg_01"


@pytest.mark.parametrize("event_type", list(EVENT_DATA))
def test_each_event_round_trips_its_wire_shape(event_type: str) -> None:
    wire = {"seq": 7, "type": event_type, "data": EVENT_DATA[event_type]}
    event = STREAM_EVENT.validate_json(json.dumps(wire))
    assert event.type == event_type
    assert json.loads(STREAM_EVENT.dump_json(event)) == wire


@pytest.mark.parametrize("event_type", [t for t in EVENT_DATA if t != "resync"])
def test_an_event_with_the_wrong_data_is_rejected(event_type: str) -> None:
    with pytest.raises(ValidationError):
        STREAM_EVENT.validate_python({"seq": 1, "type": event_type, "data": {"key": "val"}})


def test_resync_carries_no_data() -> None:
    with pytest.raises(ValidationError):
        STREAM_EVENT.validate_python({"seq": 1, "type": "resync", "data": {"key": "val"}})


def test_stream_event_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        STREAM_EVENT.validate_python({"seq": 1, "type": "invalid.type", "data": {}})


class DummySessions:
    async def get(self, project_id: str, agent: str) -> SessionView:
        return SessionView(
            id="ses_dummy",
            agent=agent,
            busy=False,
            messages=[],
            last_seq=0,
            current_task=None,
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

    async def post(
        self,
        project_id: str,
        agent: str,
        *,
        author_name: str,
        from_agent: str,
        text: str,
        exchange_id: str | None,
        hop: int | None,
    ) -> MessageView:
        return MessageView(
            id="msg_dummy",
            turn_id=None,
            author_kind="user",
            author_name=author_name,
            from_agent=from_agent,
            parts=[TextPart(type="text", text=text)],
            status="complete",
            created_at="2026-10-21T18:00:00Z",
        )

    async def update_message(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView:
        return MessageView(
            id=message_id,
            turn_id=None,
            author_kind="agent",
            author_name="dummy",
            from_agent=None,
            parts=transform([]),
            status="complete",
            created_at="2026-10-21T18:00:00Z",
        )

    async def stop(self, project_id: str, agent: str) -> None:
        pass

    async def subscribe(
        self, project_id: str, agent: str, after_seq: int | None
    ) -> AsyncGenerator[StreamEvent, None]:
        yield Resync(seq=1, type="resync", data=ResyncData())

    async def turn_states(self, project_id: str) -> dict[str, Literal["running", "waiting"]]:
        return {}

    async def ensure_recovered(self, project_id: str) -> None:
        pass

    async def resume_turn(self, project_id: str, session_id: str, turn_id: str) -> None:
        pass

    async def close_project(self, project_id: str) -> None:
        """Before a project is removed: ends its running turns (interrupted), drops its hubs and
        subscribers, closes its database handles; later calls for that project start fresh."""
        pass


def test_agent_sessions_protocol() -> None:
    sessions: AgentSessions = DummySessions()
    assert sessions is not None


def test_the_shared_fold_fixture_matches_the_contract() -> None:
    # shared/fixtures/session-fold.json is read by the hub's and the frontend's fold tests; it
    # must only hold events and messages I1 accepts.
    path = Path(__file__).parents[4] / "shared" / "fixtures" / "session-fold.json"
    doc = json.loads(path.read_text("utf-8"))
    for event in doc["events"]:
        STREAM_EVENT.validate_python(event)
    for state in [doc["end"], *doc["after_event"].values()]:
        for message in state["messages"]:
            MessageView.model_validate(message)


def test_a_waiting_message_with_an_approval_part_round_trips() -> None:
    message = MessageView(
        id="msg_02",
        turn_id="trn_01",
        author_kind="agent",
        author_name="orchestrator",
        from_agent=None,
        parts=[
            ToolCallPart(type="tool_call", id="call_1", name="Write", input={"path": "a.py"}),
            ApprovalPart(
                type="approval",
                approval_id="apr_01",
                tool_call_id="call_1",
                preview=Preview(kind="diff", title="Write a.py", body="+print('hi')"),
                status="pending",
                note=None,
            ),
        ],
        status="waiting",
        created_at="2026-10-24T09:00:00Z",
    )
    restored = MessageView.model_validate_json(message.model_dump_json())
    assert restored == message
    assert restored.status == "waiting"
    approval = restored.parts[1]
    assert isinstance(approval, ApprovalPart)
    assert approval.preview.kind == "diff"


def test_an_approval_part_stored_before_stage_3_loads_as_a_tool_approval() -> None:
    stored = {
        "type": "approval",
        "approval_id": "apr_01",
        "tool_call_id": "call_1",
        "preview": {"kind": "diff", "title": "Write a.py", "body": "+x"},
        "status": "approved",
        "note": None,
    }

    part = ApprovalPart.model_validate(stored)

    assert (part.kind, part.step, part.proposal_kind) == ("tool", None, None)


def test_a_stall_part_with_no_tool_call_round_trips() -> None:
    message = MessageView(
        id="msg_03",
        turn_id="trn_01",
        author_kind="agent",
        author_name="orchestrator",
        from_agent=None,
        parts=[
            ApprovalPart(
                type="approval",
                approval_id="apr_02",
                tool_call_id=None,
                preview=Preview(kind="text", title="Stalled: no usable key", body="key 1"),
                status="pending",
                note=None,
                kind="stall",
                step=4,
            )
        ],
        status="waiting",
        created_at="2026-10-28T09:00:00Z",
    )

    restored = MessageView.model_validate_json(message.model_dump_json())

    assert restored == message
    stall = restored.parts[0]
    assert isinstance(stall, ApprovalPart)
    assert (stall.kind, stall.step, stall.tool_call_id) == ("stall", 4, None)


def test_session_view_carries_the_current_task() -> None:
    task = TaskView(
        id="tsk_01",
        agent="orchestrator",
        title="Fix the loader",
        branch="agent/tsk_01",
        status="in_review",
        review_kind="github",
        pr_number=7,
        pr_url="https://github.com/octo/demo/pull/7",
    )
    session = SessionView(
        id="ses_01", agent="orchestrator", busy=False, messages=[], last_seq=0, current_task=task
    )
    assert SessionView.model_validate_json(session.model_dump_json()).current_task == task


def test_turn_waiting_is_a_stream_event() -> None:
    wire = {"seq": 3, "type": "turn.waiting", "data": {"turn_id": "trn_01"}}
    event = STREAM_EVENT.validate_json(json.dumps(wire))
    assert isinstance(event, TurnWaiting)
    assert json.loads(STREAM_EVENT.dump_json(event)) == wire


def test_approval_statuses_are_pending_or_one_decided_status() -> None:
    from typing import get_args

    from mlops_agents.agents.runtime.contract import ApprovalStatus, DecidedApprovalStatus

    assert get_args(ApprovalStatus) == ("pending", "approved", "rejected", "cancelled", "expired")
    assert get_args(DecidedApprovalStatus) == get_args(ApprovalStatus)[1:]
