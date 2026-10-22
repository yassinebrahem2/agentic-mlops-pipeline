from __future__ import annotations

import json

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select

from mlops_agents.agents.runtime.contract import (
    MessageView,
    TextPart,
    ToolCallPart,
)
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.tables import llm_calls, turns


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
        else:
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
) -> None:
    """Summarize older whole turns if estimated size exceeds 75% of context window."""
    turns_list = _group_turns(history)
    if len(turns_list) <= 2:
        return

    # Check for prompt tokens reported for the session's last model call
    databases = store._databases  # pyright: ignore[reportPrivateUsage]
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    llm_calls.c.prompt_tokens,
                    llm_calls.c.created_at,
                    llm_calls.c.turn_id,
                    turns.c.trigger_message_id,
                )
                .select_from(llm_calls.join(turns, llm_calls.c.turn_id == turns.c.id))
                .where(turns.c.session_id == session_id, llm_calls.c.prompt_tokens.is_not(None))
                .order_by(llm_calls.c.created_at.desc(), llm_calls.c.id.desc())
                .limit(1)
            )
        ).first()

    if row is not None and row[0] is not None:
        prompt_tokens = int(row[0])
        call_created_at = str(row[1])
        call_turn_id = str(row[2]) if row[2] is not None else None
        trigger_msg_id = str(row[3]) if row[3] is not None else None

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
        estimated_size = float(len(system_text)) / 4.0 + sum(_message_len(m) for m in history) / 4.0

    threshold = 0.75 * model.context_window
    if estimated_size <= threshold:
        return

    # Keep last 2 turns, summarize everything earlier
    turns_to_summarize = turns_list[:-2]
    compacted_through = turns_to_summarize[-1][-1].id

    old_summary, _ = await store.summary(project_id, session_id)

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
                    else:
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

    reply = await caller.call(model, prompt_messages, tools=[], on_text=_noop)
    new_summary = (
        reply.message.content
        if isinstance(reply.message.content, str)
        else "".join(str(c) for c in reply.message.content)
    )

    await store.set_summary(
        project_id,
        session_id,
        summary=new_summary,
        compacted_through=compacted_through,
    )
