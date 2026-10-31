import httpx2

from mlops_agents.agents.runtime.budget import (
    COMPACT_AT_SHARE,
    OLD_TOOL_RESULT_CHARS,
    REPLY_RESERVE_MAX_TOKENS,
    REPLY_RESERVE_SHARE,
    ROOM_FLOOR_TOKENS,
    SUMMARY_MAX_SHARE,
    TOOL_RESULT_SHARE,
    cut_text,
    fixed_tokens,
    reply_reserve_tokens,
    request_budget_tokens,
    room_tokens,
    tool_result_max_chars,
)
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.models import ModelEntry


def test_budget_constants() -> None:
    assert REPLY_RESERVE_SHARE == 0.15
    assert REPLY_RESERVE_MAX_TOKENS == 4000
    assert ROOM_FLOOR_TOKENS == 1500
    assert TOOL_RESULT_SHARE == 0.25
    assert COMPACT_AT_SHARE == 0.75
    assert SUMMARY_MAX_SHARE == 0.10
    assert OLD_TOOL_RESULT_CHARS == 500


def test_reply_reserve_tokens() -> None:
    # 8,000 budget -> 15% is 1,200
    assert reply_reserve_tokens(8000) == 1200
    # 250,000 budget -> 15% is 37,500, capped at 4,000
    assert reply_reserve_tokens(250000) == 4000


def test_fixed_tokens() -> None:
    system_text = "a" * 400
    tools = [{"type": "function", "function": {"name": "Read", "description": "b" * 300}}]
    # Estimated as total characters // 4
    fixed = fixed_tokens(system_text, tools)
    import json

    expected = (len(system_text) + len(json.dumps(tools[0]))) // 4
    assert fixed == expected


def test_room_tokens() -> None:
    model_8k = ResolvedModel(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        base_url="https://x.io",
        wire_model_id="gpt",
        context_window=64000,
        keys=(),
        request_token_limit=8000,
    )
    # 8,000 budget, 5,900 fixed: reply reserve = 1,200; room = 8,000 - 5,900 - 1,200 = 900
    assert room_tokens(model_8k, 5900) == 900
    # 8,000 budget, 2,750 fixed (Alert Prioritization): room = 8,000 - 2,750 - 1,200 = 4,050
    assert room_tokens(model_8k, 2750) == 4050

    model_250k = ResolvedModel(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        base_url="https://x.io",
        wire_model_id="gpt",
        context_window=250000,
        keys=(),
    )
    # 250,000 budget, 5,900 fixed: reply reserve = 4,000; room = 250,000 - 5,900 - 4,000 = 240,100
    assert room_tokens(model_250k, 5900) == 240100


def test_tool_result_max_chars() -> None:
    model_8k = ResolvedModel(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        base_url="https://x.io",
        wire_model_id="gpt",
        context_window=64000,
        keys=(),
        request_token_limit=8000,
    )
    # room = 4,050 tokens -> 25% of room is 1,012.5 tokens = 4,050 characters
    assert tool_result_max_chars(model_8k, 2750) == 4050

    # Negative or 0 room gives 0
    assert tool_result_max_chars(model_8k, 8000) == 0


def test_cut_text_within_limit() -> None:
    text = "Short text"
    assert cut_text(text, 100) == "Short text"


def test_cut_text_exceeding_limit() -> None:
    text = "x" * 1000
    cut = cut_text(text, 200)
    assert cut.startswith("x" * 200)
    assert "800 characters cut to fit context budget" in cut
    assert "Use offset and limit, narrower search, or grep to read smaller parts." in cut


def test_cut_text_closes_untrusted_block() -> None:
    inner = "sensitive log data line 1\nsensitive log data line 2\nsensitive log data line 3"
    tagged = untrusted(inner, source="server_log")
    # Cut in the middle of untrusted content
    cut = cut_text(tagged, 35)
    # Must close the data tag before the cut notice
    assert "</data>" in cut
    notice_idx = cut.find("characters cut to fit context budget")
    close_idx = cut.find("</data>")
    assert close_idx != -1
    assert close_idx < notice_idx


def test_request_budget_tokens() -> None:
    model_entry_no_limit = ModelEntry(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        wire_model_id="gpt",
        context_window=32768,
        tool_calling=True,
        streaming=True,
        availability="verified",
        availability_detail=None,
        verified_at=None,
        request_token_limit=None,
    )
    assert request_budget_tokens(model_entry_no_limit) == 32768

    model_entry_with_limit = ModelEntry(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        wire_model_id="gpt",
        context_window=128000,
        tool_calling=True,
        streaming=True,
        availability="verified",
        availability_detail=None,
        verified_at=None,
        request_token_limit=8000,
    )
    assert request_budget_tokens(model_entry_with_limit) == 8000

    resolved_no_limit = ResolvedModel(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        base_url="https://x.io",
        wire_model_id="gpt",
        context_window=64000,
        keys=(),
        request_token_limit=None,
    )
    assert request_budget_tokens(resolved_no_limit) == 64000

    resolved_with_limit = ResolvedModel(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        base_url="https://x.io",
        wire_model_id="gpt",
        context_window=64000,
        keys=(),
        request_token_limit=8000,
    )
    assert request_budget_tokens(resolved_with_limit) == 8000

    # When context window is smaller than limit, take context window
    resolved_small_window = ResolvedModel(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        base_url="https://x.io",
        wire_model_id="gpt",
        context_window=4000,
        keys=(),
        request_token_limit=8000,
    )
    assert request_budget_tokens(resolved_small_window) == 4000


def test_resolved_model_build_passes_max_tokens_when_limit_set() -> None:
    http = httpx2.AsyncClient()
    model_without_limit = ResolvedModel(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        base_url="https://x.io",
        wire_model_id="gpt",
        context_window=64000,
        keys=(),
        request_token_limit=None,
    )
    chat_unlimited = model_without_limit.build(http)
    assert chat_unlimited.max_tokens is None

    model_with_limit = ResolvedModel(
        address="fake:gpt",
        provider_id="p1",
        provider_name="fake",
        base_url="https://x.io",
        wire_model_id="gpt",
        context_window=64000,
        keys=(),
        request_token_limit=8000,
    )
    chat_limited = model_with_limit.build(http)
    assert chat_limited.max_tokens == 1200
