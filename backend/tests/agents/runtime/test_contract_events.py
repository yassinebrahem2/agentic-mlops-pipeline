from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from mlops_agents.agents.runtime.contract import MessageView, StreamEvent, TurnWaiting

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


def test_turn_waiting_is_a_stream_event() -> None:
    wire = {"seq": 3, "type": "turn.waiting", "data": {"turn_id": "trn_01"}}
    event = STREAM_EVENT.validate_json(json.dumps(wire))
    assert isinstance(event, TurnWaiting)
    assert json.loads(STREAM_EVENT.dump_json(event)) == wire


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
