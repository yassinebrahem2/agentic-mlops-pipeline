from __future__ import annotations

import json
import time
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from mlops_agents.agents.runtime.budget import (
    COMPACT_AT_SHARE,
    SUMMARY_MAX_SHARE,
    cut_text,
    fixed_tokens,
    reply_reserve_tokens,
    request_budget_tokens,
    room_tokens,
    tool_result_max_chars,
)
from mlops_agents.agents.runtime.contract import (
    MessageView,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.providers.bridge import ResolvedModel


def group_turns(messages: list[MessageView]) -> list[list[MessageView]]:
    grouped: list[list[MessageView]] = []
    current: list[MessageView] = []
    for msg in messages:
        if msg.status == "queued" or msg.author_kind == "system":
            continue
        if msg.author_kind == "user" and current:
            grouped.append(current)
            current = []
        current.append(msg)
    if current:
        grouped.append(current)
    return grouped


_group_turns = group_turns


def message_len(msg: MessageView) -> int:
    total = 0
    for p in msg.parts:
        if isinstance(p, TextPart):
            total += len(p.text)
        elif isinstance(p, ToolCallPart):
            total += len(p.name) + len(json.dumps(p.input))
        elif isinstance(p, ToolResultPart):
            total += len(p.content)
    return total


_message_len = message_len


async def compact_if_needed(
    store: SessionStore,
    caller: ModelCaller,
    model: ResolvedModel,
    project_id: str,
    session_id: str,
    history: list[MessageView],
    system_text: str,
    *,
    turn_id: str,
    agent_name: str,
    tools: list[dict[str, Any]] | None = None,
    force: bool = False,
) -> bool:
    """Summarize older whole turns if estimated size exceeds COMPACT_AT_SHARE of room."""
    old_summary, old_compacted_through = await store.summary(project_id, session_id)
    if old_compacted_through:
        compacted_idx = -1
        for i, m in enumerate(history):
            if m.id == old_compacted_through:
                compacted_idx = i
                break
        uncompacted_history = history[compacted_idx + 1 :] if compacted_idx >= 0 else history
    else:
        uncompacted_history = history

    uncompacted_turns = _group_turns(uncompacted_history)
    if len(uncompacted_turns) <= 2:
        return False

    fixed = fixed_tokens(system_text, tools or [])
    room = room_tokens(model, fixed)
    threshold = COMPACT_AT_SHARE * room

    if not force:
        # Check for prompt tokens reported for the session's last model call
        last_call = await store.last_llm_call_prompt_tokens(project_id, session_id)
        if last_call is not None:
            prompt_tokens, call_created_at, call_turn_id, trigger_msg_id = last_call

            last_turn_idx = -1
            for i, m in enumerate(history):
                if (call_turn_id and m.turn_id == call_turn_id) or (
                    trigger_msg_id and m.id == trigger_msg_id
                ):
                    last_turn_idx = i

            if last_turn_idx >= 0:
                messages_after = history[last_turn_idx + 1 :]
            else:
                messages_after = [m for m in history if m.created_at > call_created_at]

            conversation_tokens = (
                max(0.0, float(prompt_tokens) - fixed)
                + sum(_message_len(m) for m in messages_after) / 4.0
            )
        else:
            conversation_tokens = sum(_message_len(m) for m in uncompacted_history) / 4.0

        if conversation_tokens <= threshold:
            return False

    # Keep last 2 turns, summarize everything earlier from uncompacted turns
    turns_to_summarize = uncompacted_turns[:-2]
    new_compacted_through = turns_to_summarize[-1][-1].id

    budget = request_budget_tokens(model)
    reserve = reply_reserve_tokens(budget)
    tr_max_chars = tool_result_max_chars(model, fixed)
    max_summary_chars = max(0, int(room * SUMMARY_MAX_SHARE * 4))

    all_lines: list[str] = []
    for turn in turns_to_summarize:
        for m in turn:
            if m.author_kind == "user":
                user_text = "".join(p.text for p in m.parts if isinstance(p, TextPart))
                all_lines.append(f"User: {user_text}")
            elif m.author_kind == "agent":
                for p in m.parts:
                    if isinstance(p, TextPart):
                        all_lines.append(f"Agent: {p.text}")
                    elif isinstance(p, ToolCallPart):
                        all_lines.append(f"Agent tool call: {p.name}({p.input})")
                    elif isinstance(p, ToolResultPart):
                        trimmed = cut_text(p.content, tr_max_chars)
                        all_lines.append(f"Tool result: {trimmed}")

    instruction = (
        "Provide a concise summary of the conversation above, focusing on decisions made, "
        "facts learned about the repository, and open questions."
    )
    inst_tokens = len(instruction) // 4
    if budget >= 1500:
        max_human_tokens = budget - reserve - inst_tokens
    else:
        max_human_tokens = max(budget - reserve - inst_tokens, budget)
    max_human_chars = max(100, max_human_tokens * 4)

    current_summary = old_summary or ""

    async def _noop(_: str) -> None:
        pass

    line_idx = 0
    while line_idx < len(all_lines) or (not all_lines and old_summary and not current_summary):
        if current_summary:
            summary_prefix = f"Previous summary:\n{current_summary}\n\nConversation to summarize:\n"
        else:
            summary_prefix = "Conversation to summarize:\n"

        avail_chars = max_human_chars - len(summary_prefix)
        if avail_chars <= 0:
            if current_summary:
                current_summary = cut_text(current_summary, max(50, max_human_chars // 2))
                summary_prefix = (
                    f"Previous summary:\n{current_summary}\n\nConversation to summarize:\n"
                )
                avail_chars = max(50, max_human_chars - len(summary_prefix))
            else:
                avail_chars = max(50, max_human_chars)

        chunk_lines: list[str] = []
        chunk_len = 0
        while line_idx < len(all_lines):
            line = all_lines[line_idx]
            cost = len(line) + (1 if chunk_lines else 0)
            if chunk_len + cost <= avail_chars:
                chunk_lines.append(line)
                chunk_len += cost
                line_idx += 1
            else:
                if not chunk_lines:
                    chunk_lines.append(cut_text(line, avail_chars))
                    line_idx += 1
                break

        human_content = (
            f"{summary_prefix}{'\n'.join(chunk_lines)}" if chunk_lines else summary_prefix
        )
        prompt_messages: list[BaseMessage] = [
            SystemMessage(content=instruction),
            HumanMessage(content=human_content),
        ]

        t0 = time.monotonic()
        reply = await caller.call(model, prompt_messages, tools=[], on_text=_noop)
        latency_ms = int((time.monotonic() - t0) * 1000)

        await store.add_llm_call(
            project_id,
            turn_id,
            agent=agent_name,
            model_address=model.address,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            latency_ms=latency_ms,
        )

        new_summary = (
            reply.message.content
            if isinstance(reply.message.content, str)
            else "".join(str(c) for c in reply.message.content)
        )
        if max_summary_chars > 0 and len(new_summary) > max_summary_chars:
            new_summary = new_summary[:max_summary_chars]
        current_summary = new_summary

    await store.set_summary(
        project_id,
        session_id,
        summary=current_summary,
        compacted_through=new_compacted_through,
    )
    return True
