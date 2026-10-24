from __future__ import annotations

import json
import time

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from mlops_agents.agents.runtime.contract import (
    MessageView,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.providers.bridge import ResolvedModel


def _group_turns(messages: list[MessageView]) -> list[list[MessageView]]:
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


def _message_len(msg: MessageView) -> int:
    total = 0
    for p in msg.parts:
        if isinstance(p, TextPart):
            total += len(p.text)
        elif isinstance(p, ToolCallPart):
            total += len(p.name) + len(json.dumps(p.input))
        elif isinstance(p, ToolResultPart):
            total += len(p.content)
    return total


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
) -> None:
    """Summarize older whole turns if estimated size exceeds 75% of context window."""
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
        return

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

        estimated_size = float(prompt_tokens) + sum(_message_len(m) for m in messages_after) / 4.0
    else:
        # Count only messages after compacted_through, plus system_text (which holds previous summary)
        estimated_size = (
            float(len(system_text)) / 4.0 + sum(_message_len(m) for m in uncompacted_history) / 4.0
        )

    threshold = 0.75 * model.context_window
    if estimated_size <= threshold:
        return

    # Keep last 2 turns, summarize everything earlier from uncompacted turns
    turns_to_summarize = uncompacted_turns[:-2]
    new_compacted_through = turns_to_summarize[-1][-1].id

    lines: list[str] = []
    if old_summary:
        lines.append(f"Previous summary:\n{old_summary}\n")

    lines.append("Conversation to summarize:")
    for turn in turns_to_summarize:
        for m in turn:
            if m.author_kind == "user":
                user_text = "".join(p.text for p in m.parts if isinstance(p, TextPart))
                lines.append(f"User: {user_text}")
            elif m.author_kind == "agent":
                for p in m.parts:
                    if isinstance(p, TextPart):
                        lines.append(f"Agent: {p.text}")
                    elif isinstance(p, ToolCallPart):
                        lines.append(f"Agent tool call: {p.name}({p.input})")
                    elif isinstance(p, ToolResultPart):
                        lines.append(f"Tool result: {p.content}")

    instruction = (
        "Provide a concise summary of the conversation above, focusing on decisions made, "
        "facts learned about the repository, and open questions."
    )
    prompt_messages: list[BaseMessage] = [
        SystemMessage(content=instruction),
        HumanMessage(content="\n".join(lines)),
    ]

    async def _noop(_: str) -> None:
        pass

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

    await store.set_summary(
        project_id,
        session_id,
        summary=new_summary,
        compacted_through=new_compacted_through,
    )
