from typing import Any

import pytest

from mlops_agents.providers.models import ModelEntry

USABLE: dict[str, Any] = {
    "address": "groq/m1",
    "provider_id": "prv_1",
    "provider_name": "groq",
    "wire_model_id": "m1",
    "context_window": 8192,
    "tool_calling": True,
    "streaming": True,
    "availability": "verified",
    "availability_detail": None,
    "verified_at": "2026-10-21T00:00:00Z",
}


def test_a_verified_model_with_both_traits_and_a_context_window_has_no_problem() -> None:
    assert ModelEntry.model_validate(USABLE).problem is None


@pytest.mark.parametrize(
    "change",
    [{"availability": "untested"}, {"tool_calling": False}, {"streaming": False}],
)
def test_an_unverified_model_says_so(change: dict[str, Any]) -> None:
    assert ModelEntry.model_validate({**USABLE, **change}).problem == (
        "The model groq/m1 has not passed verification (tool calls and streaming). "
        "Verify it in AI providers or choose another model."
    )


def test_a_failed_model_gives_the_recorded_failure() -> None:
    entry = ModelEntry.model_validate(
        {**USABLE, "availability": "needs_attention", "availability_detail": "HTTP 404"}
    )
    assert entry.problem == (
        "The model groq/m1 failed: HTTP 404. Verify it again in AI providers or choose another model."
    )


def test_an_unknown_context_window_is_a_problem() -> None:
    assert ModelEntry.model_validate({**USABLE, "context_window": None}).problem == (
        "The context window of groq/m1 is unknown. Enter it in AI providers."
    )


def test_the_problem_is_serialized_for_the_api() -> None:
    assert (
        ModelEntry.model_validate({**USABLE, "context_window": None}).model_dump()["problem"]
        is not None
    )
