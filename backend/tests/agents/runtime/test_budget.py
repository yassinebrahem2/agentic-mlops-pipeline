import httpx2

from mlops_agents.agents.runtime.budget import (
    REPLY_RESERVE_MAX_TOKENS,
    REPLY_RESERVE_SHARE,
    reply_reserve_tokens,
    request_budget_tokens,
)
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.models import ModelEntry


def test_reply_reserve_constants() -> None:
    assert REPLY_RESERVE_SHARE == 0.15
    assert REPLY_RESERVE_MAX_TOKENS == 4000


def test_reply_reserve_tokens() -> None:
    # 8,000 budget -> 15% is 1,200
    assert reply_reserve_tokens(8000) == 1200
    # 250,000 budget -> 15% is 37,500, capped at 4,000
    assert reply_reserve_tokens(250000) == 4000


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
