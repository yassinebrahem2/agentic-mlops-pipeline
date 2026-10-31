"""Every read Stats makes of stage 2's tables: read-only SQL, one function per number.

Known debt: the owners of `approvals`, `tasks`, `turns`, `messages`, `llm_calls` and `events`
have no aggregate read, so these queries read the tables directly; a later read API replaces
only this module. Stage 2 rows are stamped with `to_iso` text, so text bounds are time bounds.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Column, ColumnElement, Table, func, select

from mlops_agents.clock import to_iso
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import approvals, events, llm_calls, messages, tasks, turns


@dataclass(frozen=True)
class Span:
    """A stats window: `[start, end]`, both inclusive; no `start` has no lower bound."""

    start: datetime | None
    end: datetime

    def contains(self, at: datetime) -> bool:
        return (self.start is None or self.start <= at) and at <= self.end

    def bounds(self, column: Column[Any]) -> list[ColumnElement[bool]]:
        found = [column <= to_iso(self.end)]
        if self.start is not None:
            found.append(column >= to_iso(self.start))
        return found


async def _count(
    databases: Databases, project_id: str, table: Table, where: Sequence[ColumnElement[bool]]
) -> int:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        return int((await conn.scalar(select(func.count()).select_from(table).where(*where))) or 0)


async def approvals_created(databases: Databases, project_id: str, span: Span) -> int:
    return await _count(databases, project_id, approvals, span.bounds(approvals.c.created_at))


async def approvals_approved(databases: Databases, project_id: str, span: Span) -> int:
    """Approvals created in the window and approved."""
    where = [approvals.c.status == "approved", *span.bounds(approvals.c.created_at)]
    return await _count(databases, project_id, approvals, where)


async def approvals_rejected(databases: Databases, project_id: str, span: Span) -> int:
    """Approvals created in the window and rejected."""
    where = [approvals.c.status == "rejected", *span.bounds(approvals.c.created_at)]
    return await _count(databases, project_id, approvals, where)


async def prs_opened(databases: Databases, project_id: str, span: Span) -> int:
    """Agents' tasks with a PR, created in the window."""
    where = [tasks.c.pr_number.is_not(None), *span.bounds(tasks.c.created_at)]
    return await _count(databases, project_id, tasks, where)


async def prs_merged(databases: Databases, project_id: str, span: Span) -> int:
    """Agents' tasks with a PR, merged in the window (a local review merges without one)."""
    where = [
        tasks.c.pr_number.is_not(None),
        tasks.c.status == "merged",
        *span.bounds(tasks.c.ended_at),
    ]
    return await _count(databases, project_id, tasks, where)


async def prs_closed(databases: Databases, project_id: str, span: Span) -> int:
    """Agents' tasks with a PR, closed unmerged in the window."""
    where = [
        tasks.c.pr_number.is_not(None),
        tasks.c.status == "closed",
        *span.bounds(tasks.c.ended_at),
    ]
    return await _count(databases, project_id, tasks, where)


async def failed_turns(databases: Databases, project_id: str, span: Span) -> int:
    where = [turns.c.status == "failed", *span.bounds(turns.c.created_at)]
    return await _count(databases, project_id, turns, where)


async def sandbox_build_failures(databases: Databases, project_id: str, span: Span) -> int:
    where = [events.c.type == "sandbox.build.failed", *span.bounds(events.c.ts)]
    return await _count(databases, project_id, events, where)


async def tool_errors(databases: Databases, project_id: str, span: Span) -> int:
    """`tool_result` parts with `is_error` in the window's messages, read from the parsed parts
    so the JSON's key order never hides one."""
    stmt = select(messages.c.parts).where(
        messages.c.parts.contains("tool_result"), *span.bounds(messages.c.created_at)
    )
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).scalars().all()
    found = 0
    for raw in rows:
        parts = cast(list[dict[str, Any]], json.loads(str(raw)))
        found += sum(1 for p in parts if p.get("type") == "tool_result" and p.get("is_error"))
    return found


async def tokens_by_agent(databases: Databases, project_id: str, span: Span) -> dict[str, int]:
    """Prompt plus completion tokens per agent with calls in the window."""
    tokens = func.sum(
        func.coalesce(llm_calls.c.prompt_tokens, 0)
        + func.coalesce(llm_calls.c.completion_tokens, 0)
    )
    stmt = (
        select(llm_calls.c.agent, tokens.label("tokens"))
        .where(*span.bounds(llm_calls.c.created_at))
        .group_by(llm_calls.c.agent)
    )
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return {str(row.agent): int(row.tokens) for row in rows}


async def tokens_by_model(
    databases: Databases, project_id: str, span: Span
) -> dict[str, tuple[int, int]]:
    """`(prompt, completion)` tokens per model address with calls in the window."""
    stmt = (
        select(
            llm_calls.c.model_address,
            func.sum(func.coalesce(llm_calls.c.prompt_tokens, 0)).label("prompt"),
            func.sum(func.coalesce(llm_calls.c.completion_tokens, 0)).label("completion"),
        )
        .where(*span.bounds(llm_calls.c.created_at))
        .group_by(llm_calls.c.model_address)
    )
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return {str(row.model_address): (int(row.prompt), int(row.completion)) for row in rows}
