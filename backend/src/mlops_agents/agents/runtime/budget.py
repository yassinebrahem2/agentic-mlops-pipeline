"""Budget and size limits for agent execution (stage 2 context budget plan).

All sizing rules live here as named constants with their rationale; tokens are
estimated as characters / 4.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# 15% of the budget, at most 4,000 tokens. Groq and Gemini count requested output
# against the TPM limit, so this bounds max_tokens for models with a request limit.
REPLY_RESERVE_SHARE = 0.15
REPLY_RESERVE_MAX_TOKENS = 4000


@runtime_checkable
class HasContextBudget(Protocol):
    context_window: int | None
    request_token_limit: int | None


def request_budget_tokens(model: HasContextBudget | Any) -> int:
    """The smaller of the model's context window and the person's request limit.

    When the request limit is None, returns the context window.
    """
    limit: int | None = getattr(model, "request_token_limit", None)
    window: int | None = getattr(model, "context_window", None)

    if limit is not None:
        if window is not None:
            return min(window, limit)
        return limit

    if window is None:
        raise ValueError(f"Model {model} has no context window or request limit set.")
    return window


def reply_reserve_tokens(budget: int) -> int:
    """The reply reserve: 15% of the budget, at most 4,000 tokens."""
    return min(int(budget * REPLY_RESERVE_SHARE), REPLY_RESERVE_MAX_TOKENS)
