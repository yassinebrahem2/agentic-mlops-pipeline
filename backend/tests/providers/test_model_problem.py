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


@pytest.mark.parametrize(
    ("change", "starts"),
    [
        (
            {"availability": "needs_attention", "availability_detail": "x", "context_window": None},
            "The model groq/m1 failed",
        ),
        ({"availability": "untested", "context_window": None}, "The model groq/m1 has not passed"),
    ],
)
def test_the_first_failing_rule_gives_the_message(change: dict[str, Any], starts: str) -> None:
    problem = ModelEntry.model_validate({**USABLE, **change}).problem
    assert problem is not None and problem.startswith(starts)


def test_the_problem_is_serialized_for_the_api() -> None:
    assert (
        ModelEntry.model_validate({**USABLE, "context_window": None}).model_dump()["problem"]
        is not None
    )


USABLE_EMBEDDER: dict[str, Any] = {
    "address": "fake:fake-embed",
    "provider_id": "prv_fake",
    "provider_name": "fake",
    "wire_model_id": "fake-embed",
    "context_window": None,
    "tool_calling": False,
    "streaming": False,
    "availability": "verified",
    "availability_detail": None,
    "verified_at": "2026-10-28T00:00:00Z",
    "model_type": "embedder",
    "dimensions": 16,
}


def test_a_verified_embedder_with_dimensions_has_no_problem() -> None:
    assert ModelEntry.model_validate(USABLE_EMBEDDER).problem is None


def test_an_unverified_embedder_says_so() -> None:
    assert ModelEntry.model_validate({**USABLE_EMBEDDER, "availability": "untested"}).problem == (
        "The embedder fake:fake-embed has not passed verification (an embeddings call). "
        "Verify it in AI providers or choose another model."
    )


def test_a_verified_embedder_with_null_dimensions_is_a_problem() -> None:
    assert ModelEntry.model_validate({**USABLE_EMBEDDER, "dimensions": None}).problem == (
        "The embedder fake:fake-embed has not passed verification (an embeddings call). "
        "Verify it in AI providers or choose another model."
    )


def test_a_failed_embedder_gives_recorded_failure() -> None:
    entry = ModelEntry.model_validate(
        {**USABLE_EMBEDDER, "availability": "needs_attention", "availability_detail": "HTTP 401"}
    )
    assert entry.problem == (
        "The model fake:fake-embed failed: HTTP 401. Verify it again in AI providers or choose another model."
    )


def test_problem_for_wanted_type() -> None:
    chat_entry = ModelEntry.model_validate(USABLE)
    embedder_entry = ModelEntry.model_validate(USABLE_EMBEDDER)

    assert chat_entry.problem_for("chat") is None
    assert embedder_entry.problem_for("embedder") is None

    assert chat_entry.problem_for("embedder") == (
        "groq/m1 is a chat model, not an embedder model. Choose an embedder model."
    )
    assert embedder_entry.problem_for("chat") == (
        "fake:fake-embed is an embedder model, not a chat model. Choose a chat model."
    )
