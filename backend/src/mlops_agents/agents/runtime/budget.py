"""Budget and size limits for agent execution (stage 2 context budget plan).

All sizing rules live here as named constants with their rationale; tokens are
estimated as characters / 4.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from mlops_agents.monitoring.untrusted import DATA_CLOSE_TAG

# 15% of the budget, at most 4,000 tokens. Groq and Gemini count requested output
# against the TPM limit, so this bounds max_tokens for models with a request limit.
REPLY_RESERVE_SHARE = 0.15
REPLY_RESERVE_MAX_TOKENS = 4000

# Below 1,500 tokens of room, the turn is refused at once with a message naming the
# agent, the model, its limit and the fixed part's size.
ROOM_FLOOR_TOKENS = 1500

# Tool results are capped at 25% of the room, in characters (tokens * 4).
TOOL_RESULT_SHARE = 0.25

# Compaction starts when the conversation part passes 75% of the room.
COMPACT_AT_SHARE = 0.75

# The summary length is at most 10% of the room.
SUMMARY_MAX_SHARE = 0.10

# Older tool output re-sent in history messages is capped at 500 characters plus notice.
OLD_TOOL_RESULT_CHARS = 500

NOTICE_SUFFIX = (
    "characters cut to fit context budget. Use offset and limit, narrower search, "
    "or grep to read smaller parts.]"
)


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


def fixed_tokens(system_text: str, tools: list[dict[str, Any]]) -> int:
    """The request's fixed part from what is actually sent.

    Tokens are estimated as characters / 4.
    """
    total_chars = len(system_text) + sum(len(json.dumps(t)) for t in tools)
    return total_chars // 4


def room_tokens(model: HasContextBudget | Any, fixed: int) -> int:
    """The room for conversation and tool results: budget - fixed - reply_reserve."""
    budget = request_budget_tokens(model)
    reserve = reply_reserve_tokens(budget)
    return budget - fixed - reserve


def tool_result_max_chars(model: HasContextBudget | Any, fixed: int) -> int:
    """Tool result cap: 25% of the room, in characters (tokens * 4)."""
    room = room_tokens(model, fixed)
    return max(0, int(room * TOOL_RESULT_SHARE * 4))


def cut_text(text: str, max_chars: int) -> str:
    """Keeps the head of text up to max_chars and appends a notice indicating truncation.

    If the cut falls inside an open <data source="..."> block from
    mlops_agents.monitoring.untrusted, it closes the block before appending the notice.
    """
    if len(text) <= max_chars:
        return text

    head = text[: max(0, max_chars)]
    cut_count = len(text) - max(0, max_chars)

    # Check if <data was opened without being closed in head
    open_idx = head.rfind("<data")
    close_idx = head.rfind(DATA_CLOSE_TAG)
    if open_idx != -1 and (close_idx == -1 or open_idx > close_idx):
        if not head.endswith("\n"):
            head = f"{head}\n{DATA_CLOSE_TAG}\n"
        else:
            head = f"{head}{DATA_CLOSE_TAG}\n"

    notice = f"\n[... {cut_count} {NOTICE_SUFFIX}"
    return f"{head}{notice}"
