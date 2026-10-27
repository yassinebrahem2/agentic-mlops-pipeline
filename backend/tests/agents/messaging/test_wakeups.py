from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from sqlalchemy import insert, select

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.messaging.wakeups import (
    WakeupService,
)
from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import FileAccess, RiskClass, ToolContext
from mlops_agents.agents.tools.schedule_wakeup import ScheduleWakeupTool
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import sessions, wakeups
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


def _ctx(
    project_id: str,
    agent: str,
    session_id: str = "ses_test",
    turn_id: str = "trn_test",
) -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id=session_id,
        turn_id=turn_id,
        files=cast(FileAccess, object()),
        task=None,
    )


def _setup_agent_defs(tmp_path: Path) -> Path:
    defs_dir = tmp_path / "defs"
    defs_dir.mkdir(exist_ok=True)
    (defs_dir / "sched.md").write_text(
        "---\n"
        "name: sched\n"
        "display_name: Sched\n"
        "description: Scheduling test agent\n"
        "tools: [ScheduleWakeup]\n"
        "schedules: true\n"
        "---\n"
        "You are sched.\n",
        encoding="utf-8",
    )
    (defs_dir / "plain.md").write_text(
        "---\n"
        "name: plain\n"
        "display_name: Plain\n"
        "description: Plain test agent\n"
        "tools: [ScheduleWakeup]\n"
        "schedules: false\n"
        "---\n"
        "You are plain.\n",
        encoding="utf-8",
    )
    return defs_dir


async def test_schedule_wakeup_tool_metadata() -> None:
    assert ScheduleWakeupTool.name == "ScheduleWakeup"
    assert ScheduleWakeupTool.risk is RiskClass.MESSAGE


async def test_schedule_wakeup_refused_without_schedules_permission(
    tmp_path: Path,
    databases: Databases,
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: now)
    tool = ScheduleWakeupTool(wakeups_svc)
    registry = default_registry(wakeups=wakeups_svc)
    catalog = AgentCatalog.load(registry.names(), extra_dirs=[defs_dir])
    wakeups_svc.use_catalog(catalog)

    ctx = _ctx("prj_test", "plain")
    out = await tool.invoke(ctx, {"delay_seconds": 120, "note": "Check status"})
    assert out.is_error
    assert "not allowed to schedule wake-ups" in out.content


async def test_limits_enforced_delay_bounds(
    tmp_path: Path,
    databases: Databases,
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: now)
    tool = ScheduleWakeupTool(wakeups_svc)
    registry = default_registry(wakeups=wakeups_svc)
    catalog = AgentCatalog.load(registry.names(), extra_dirs=[defs_dir])
    wakeups_svc.use_catalog(catalog)

    ctx = _ctx("prj_test", "sched")

    # delay < 60s
    out_too_short = await tool.invoke(ctx, {"delay_seconds": 30, "note": "Too short"})
    assert out_too_short.is_error

    # delay > 7 days (7 * 86400 = 604800)
    out_too_long = await tool.invoke(ctx, {"delay_seconds": 700000, "note": "Too long"})
    assert out_too_long.is_error


async def test_limits_enforced_max_pending(
    tmp_path: Path,
    databases: Databases,
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: now)
    tool = ScheduleWakeupTool(wakeups_svc)
    registry = default_registry(wakeups=wakeups_svc)
    catalog = AgentCatalog.load(registry.names(), extra_dirs=[defs_dir])
    wakeups_svc.use_catalog(catalog)

    engine = await databases.project("prj_test")
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id="ses_test",
                agent="sched",
                summary=None,
                compacted_through=None,
                created_at="2026-10-26T12:00:00.000Z",
            )
        )

    ctx = _ctx("prj_test", "sched")

    # Schedule 5 pending wake-ups successfully
    for i in range(5):
        out = await tool.invoke(ctx, {"delay_seconds": 100 + i, "note": f"Wakeup {i}"})
        assert not out.is_error, f"Failed at {i}: {out.content}"

    # 6th attempt must be refused
    out_6th = await tool.invoke(ctx, {"delay_seconds": 200, "note": "Wakeup 6"})
    assert out_6th.is_error
    assert "limit of 5 pending" in out_6th.content


async def test_limits_enforced_max_fired_per_rolling_day(
    tmp_path: Path,
    databases: Databases,
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: now)
    tool = ScheduleWakeupTool(wakeups_svc)
    registry = default_registry(wakeups=wakeups_svc)
    catalog = AgentCatalog.load(registry.names(), extra_dirs=[defs_dir])
    wakeups_svc.use_catalog(catalog)

    engine = await databases.project("prj_test")
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id="ses_test",
                agent="sched",
                summary=None,
                compacted_through=None,
                created_at="2026-10-26T12:00:00.000Z",
            )
        )
        for i in range(24):
            fired_at = (
                (now - timedelta(hours=i % 20))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            await conn.execute(
                insert(wakeups).values(
                    id=f"wak_old_{i}",
                    session_id="ses_test",
                    agent="sched",
                    note=f"Prior note {i}",
                    due_at=fired_at,
                    status="fired",
                    created_at=fired_at,
                    fired_at=fired_at,
                )
            )

    ctx = _ctx("prj_test", "sched")

    # 25th in 24 hours is refused
    out = await tool.invoke(ctx, {"delay_seconds": 120, "note": "Exceeds daily limit"})
    assert out.is_error
    assert "24" in out.content

    # Advance clock past 24 hours from the oldest seeded
    future_now = now + timedelta(days=2)
    wakeups_svc_future = WakeupService(databases, clock=lambda: future_now)
    wakeups_svc_future.use_catalog(catalog)
    tool_future = ScheduleWakeupTool(wakeups_svc_future)

    out_future = await tool_future.invoke(
        ctx, {"delay_seconds": 120, "note": "Allowed after rolling day"}
    )
    assert not out_future.is_error


async def test_wakeup_due_during_downtime_fires_at_startup(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    current_time = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)

    wakeups_svc = WakeupService(databases, clock=lambda: current_time)
    tools = default_registry(wakeups=wakeups_svc)

    session_env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[defs_dir],
        tools=tools,
    )
    await session_env.settings.set_default_model("sched", f"{session_env.provider}:fake-tools")
    catalog = session_env.manager._catalog  # pyright: ignore[reportPrivateUsage]
    wakeups_svc.use_catalog(catalog)
    wakeups_svc.bind(session_env.manager)

    # Insert a wakeup due in the past (downtime wakeup)
    past_due = (
        (current_time - timedelta(minutes=5))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    engine = await databases.project(session_env.project_id)
    session_id = await session_env.store.session_id(session_env.project_id, "sched")

    async with engine.begin() as conn:
        await conn.execute(
            insert(wakeups).values(
                id="wak_downtime_1",
                session_id=session_id,
                agent="sched",
                note="Downtime reminder",
                due_at=past_due,
                status="pending",
                created_at=past_due,
                fired_at=None,
            )
        )

    sub = session_env.manager.subscribe(session_env.project_id, "sched", after_seq=None)
    await anext(sub)

    # Start wakeup service: must fire downtime wakeups immediately at startup
    await wakeups_svc.start()

    events = await collect_until_turn_ended(sub)
    await wakeups_svc.close()
    await session_env.manager.close()

    assert any(e.__class__.__name__ == "TurnEnded" for e in events)

    # Check that status was updated to fired in DB
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(wakeups.c.status, wakeups.c.fired_at).where(
                        wakeups.c.id == "wak_downtime_1"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "fired"
        assert row["fired_at"] is not None


async def test_wakeup_fires_and_starts_turn_with_note(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    current_time = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)

    def clock() -> datetime:
        return current_time

    events_log = SqliteEventLog(databases)
    wakeups_svc = WakeupService(databases, events=events_log, clock=clock)
    tools = default_registry(wakeups=wakeups_svc)

    session_env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[defs_dir],
        tools=tools,
    )
    await session_env.settings.set_default_model("sched", f"{session_env.provider}:fake-tools")
    catalog = session_env.manager._catalog  # pyright: ignore[reportPrivateUsage]
    wakeups_svc.use_catalog(catalog)
    wakeups_svc.bind(session_env.manager)

    session_id = await session_env.store.session_id(session_env.project_id, "sched")
    ctx = _ctx(session_env.project_id, "sched", session_id=session_id)
    tool = ScheduleWakeupTool(wakeups_svc, catalog=catalog)

    # Schedule wakeup for 120 seconds in future
    res = await tool.invoke(ctx, {"delay_seconds": 120, "note": "Check deployment metrics"})
    assert not res.is_error
    assert "Scheduled wake-up in 120 seconds." in res.content

    # Verify event logged
    recorded_events = await events_log.history(session_env.project_id, types=["wakeup.scheduled"])
    assert len(recorded_events) == 1
    assert recorded_events[0].source == "sched"
    assert recorded_events[0].type == "wakeup.scheduled"
    assert "wakeup_id" in recorded_events[0].payload

    # Start wakeup timer
    await wakeups_svc.start()

    sub = session_env.manager.subscribe(session_env.project_id, "sched", after_seq=None)
    await anext(sub)

    # Advance clock and wake timer
    current_time += timedelta(seconds=125)
    wakeups_svc.notify_sooner()

    # Collect events until turn completes
    events = await collect_until_turn_ended(sub)

    await wakeups_svc.close()
    await session_env.manager.close()

    # Verify turn completed
    assert any(e.__class__.__name__ == "TurnEnded" for e in events)

    # Verify model received request with the note
    assert len(fake_openai.state.requests) > 0
    all_user_messages: list[str] = []
    for req in fake_openai.state.requests:
        for m in req.get("messages", []):
            if m.get("role") == "user":
                all_user_messages.append(str(m.get("content", "")))

    assert any("Scheduled wake-up: Check deployment metrics" in msg for msg in all_user_messages)


async def test_cancel_wakeup(
    tmp_path: Path,
    databases: Databases,
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    current_time = now

    wakeups_svc = WakeupService(databases, clock=lambda: current_time)
    registry = default_registry(wakeups=wakeups_svc)
    catalog = AgentCatalog.load(registry.names(), extra_dirs=[defs_dir])
    wakeups_svc.use_catalog(catalog)

    engine = await databases.project("prj_test")
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id="ses_test",
                agent="sched",
                summary=None,
                compacted_through=None,
                created_at="2026-10-26T12:00:00.000Z",
            )
        )

    wak_id = await wakeups_svc.schedule(
        "prj_test",
        session_id="ses_test",
        agent="sched",
        delay_seconds=120,
        note="To be cancelled",
    )

    # First cancel should return True
    assert await wakeups_svc.cancel("prj_test", wak_id) is True

    # Subsequent cancel should return False
    assert await wakeups_svc.cancel("prj_test", wak_id) is False

    # Non-existent cancel should return False
    assert await wakeups_svc.cancel("prj_test", "wak_nonexistent") is False

    # Check status in db is cancelled
    async with engine.connect() as conn:
        status = await conn.scalar(select(wakeups.c.status).where(wakeups.c.id == wak_id))
        assert status == "cancelled"

    # Advancing clock past due time should NOT fire the cancelled wake-up
    current_time += timedelta(minutes=5)
    fired_count = await wakeups_svc.fire_due()
    assert fired_count == 0


async def test_list_pending(
    tmp_path: Path,
    databases: Databases,
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    (defs_dir / "sched2.md").write_text(
        "---\n"
        "name: sched2\n"
        "display_name: Sched2\n"
        "description: Second scheduling agent\n"
        "tools: [ScheduleWakeup]\n"
        "schedules: true\n"
        "---\n"
        "You are sched2.\n",
        encoding="utf-8",
    )
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: now)
    registry = default_registry(wakeups=wakeups_svc)
    catalog = AgentCatalog.load(registry.names(), extra_dirs=[defs_dir])
    wakeups_svc.use_catalog(catalog)

    engine = await databases.project("prj_test")
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id="ses_test1",
                agent="sched",
                summary=None,
                compacted_through=None,
                created_at="2026-10-26T12:00:00.000Z",
            )
        )
        await conn.execute(
            insert(sessions).values(
                id="ses_test2",
                agent="sched2",
                summary=None,
                compacted_through=None,
                created_at="2026-10-26T12:00:00.000Z",
            )
        )

    # Schedule wakeups with different delays
    id1 = await wakeups_svc.schedule(
        "prj_test",
        session_id="ses_test1",
        agent="sched",
        delay_seconds=300,
        note="Second due",
    )
    id2 = await wakeups_svc.schedule(
        "prj_test",
        session_id="ses_test2",
        agent="sched2",
        delay_seconds=100,
        note="First due",
    )

    # list_pending returns both, ordered by due_at ASC (id2 before id1)
    all_pending = await wakeups_svc.list_pending("prj_test")
    assert len(all_pending) == 2
    assert all_pending[0].id == id2
    assert all_pending[0].agent == "sched2"
    assert all_pending[1].id == id1
    assert all_pending[1].agent == "sched"

    # Filter by agent
    sched2_pending = await wakeups_svc.list_pending("prj_test", agent="sched2")
    assert len(sched2_pending) == 1
    assert sched2_pending[0].id == id2

    # Cancel id2 -> should no longer appear in list_pending
    assert await wakeups_svc.cancel("prj_test", id2) is True
    remaining = await wakeups_svc.list_pending("prj_test")
    assert len(remaining) == 1
    assert remaining[0].id == id1


async def test_note_length_validation(
    tmp_path: Path,
    databases: Databases,
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: now)
    tool = ScheduleWakeupTool(wakeups_svc)
    registry = default_registry(wakeups=wakeups_svc)
    catalog = AgentCatalog.load(registry.names(), extra_dirs=[defs_dir])
    wakeups_svc.use_catalog(catalog)

    engine = await databases.project("prj_test")
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id="ses_test",
                agent="sched",
                summary=None,
                compacted_through=None,
                created_at="2026-10-26T12:00:00.000Z",
            )
        )

    ctx = _ctx("prj_test", "sched")

    # Note exceeding 500 characters
    long_note = "a" * 501
    out_tool = await tool.invoke(ctx, {"delay_seconds": 120, "note": long_note})
    assert out_tool.is_error

    with pytest.raises(ValueError, match="500 characters"):
        await wakeups_svc.schedule(
            "prj_test",
            session_id="ses_test",
            agent="sched",
            delay_seconds=120,
            note=long_note,
        )

    # Empty/whitespace note
    out_empty = await tool.invoke(ctx, {"delay_seconds": 120, "note": "   "})
    assert out_empty.is_error

    with pytest.raises(ValueError, match="empty"):
        await wakeups_svc.schedule(
            "prj_test",
            session_id="ses_test",
            agent="sched",
            delay_seconds=120,
            note="   ",
        )

    # Valid note (exactly 500 characters)
    max_note = "b" * 500
    out_valid = await tool.invoke(ctx, {"delay_seconds": 120, "note": max_note})
    assert not out_valid.is_error


async def test_fire_due_retains_pending_if_post_fails(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    current_time = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: current_time)
    tools = default_registry(wakeups=wakeups_svc)

    session_env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[defs_dir],
        tools=tools,
    )
    catalog = session_env.manager._catalog  # pyright: ignore[reportPrivateUsage]
    wakeups_svc.use_catalog(catalog)
    wakeups_svc.bind(session_env.manager)

    session_id = await session_env.store.session_id(session_env.project_id, "sched")
    ctx = _ctx(session_env.project_id, "sched", session_id=session_id)
    tool = ScheduleWakeupTool(wakeups_svc, catalog=catalog)

    res = await tool.invoke(ctx, {"delay_seconds": 60, "note": "Retry test note"})
    assert not res.is_error

    # Make post raise an unexpected exception
    async def failing_post(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Transient delivery error")

    real_post = session_env.manager.post
    monkeypatch.setattr(session_env.manager, "post", failing_post)

    # Advance clock past due
    current_time += timedelta(seconds=65)

    # fire_due should attempt to post, catch error, and leave wakeup as pending
    fired = await wakeups_svc.fire_due()
    assert fired == 0

    pending = await wakeups_svc.list_pending(session_env.project_id)
    assert len(pending) == 1
    assert pending[0].status == "pending"

    # Restore post and retry fire_due
    monkeypatch.setattr(session_env.manager, "post", real_post)
    fired = await wakeups_svc.fire_due()
    assert fired == 1

    pending = await wakeups_svc.list_pending(session_env.project_id)
    assert len(pending) == 0

    await session_env.manager.close()


async def test_fire_due_guard_prevents_double_posting_on_restart(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    current_time = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: current_time)
    tools = default_registry(wakeups=wakeups_svc)

    session_env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[defs_dir],
        tools=tools,
    )
    catalog = session_env.manager._catalog  # pyright: ignore[reportPrivateUsage]
    wakeups_svc.use_catalog(catalog)
    wakeups_svc.bind(session_env.manager)

    session_id = await session_env.store.session_id(session_env.project_id, "sched")
    ctx = _ctx(session_env.project_id, "sched", session_id=session_id)
    tool = ScheduleWakeupTool(wakeups_svc, catalog=catalog)

    # Schedule wakeup
    res = await tool.invoke(ctx, {"delay_seconds": 60, "note": "Idempotency test note"})
    assert not res.is_error

    pending_before = await wakeups_svc.list_pending(session_env.project_id)
    assert len(pending_before) == 1
    wak_id = pending_before[0].id

    # Simulate crash right after post: post the wake-up message to the session, but leave wakeup pending
    await session_env.manager.post(
        session_env.project_id,
        "sched",
        author_name="Scheduled wake-up",
        from_agent="sched",
        text=f"Scheduled wake-up: Idempotency test note [{wak_id}]",
        exchange_id=None,
        hop=None,
    )

    # Advance clock past due
    current_time += timedelta(seconds=65)

    # fire_due runs (as if on restart)
    fired = await wakeups_svc.fire_due()
    assert fired == 1

    # Status must be fired
    pending = await wakeups_svc.list_pending(session_env.project_id)
    assert len(pending) == 0

    # Verify no second message was posted: messages table has exactly 1 wake-up message
    msgs = await session_env.store.messages(session_env.project_id, session_id)
    wakeup_msgs = [m for m in msgs if m.author_name == "Scheduled wake-up"]
    assert len(wakeup_msgs) == 1

    await session_env.manager.close()


async def test_fire_due_two_pending_wakeups_same_note_both_post_and_fire(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    defs_dir = _setup_agent_defs(tmp_path)
    current_time = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    wakeups_svc = WakeupService(databases, clock=lambda: current_time)
    tools = default_registry(wakeups=wakeups_svc)

    session_env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[defs_dir],
        tools=tools,
    )
    catalog = session_env.manager._catalog  # pyright: ignore[reportPrivateUsage]
    wakeups_svc.use_catalog(catalog)
    wakeups_svc.bind(session_env.manager)

    session_id = await session_env.store.session_id(session_env.project_id, "sched")
    ctx = _ctx(session_env.project_id, "sched", session_id=session_id)
    tool = ScheduleWakeupTool(wakeups_svc, catalog=catalog)

    # Schedule two wakeups with the exact same note ("Check CI")
    res1 = await tool.invoke(ctx, {"delay_seconds": 60, "note": "Check CI"})
    assert not res1.is_error
    res2 = await tool.invoke(ctx, {"delay_seconds": 60, "note": "Check CI"})
    assert not res2.is_error

    pending_before = await wakeups_svc.list_pending(session_env.project_id)
    assert len(pending_before) == 2
    assert pending_before[0].id != pending_before[1].id

    # Advance clock past due
    current_time += timedelta(seconds=65)

    # fire_due runs: both fall due in the same pass
    fired = await wakeups_svc.fire_due()
    assert fired == 2

    # Both wake-ups end fired (no pending wake-ups left)
    pending_after = await wakeups_svc.list_pending(session_env.project_id)
    assert len(pending_after) == 0

    # Both messages must be posted in the session
    msgs = await session_env.store.messages(session_env.project_id, session_id)
    wakeup_msgs = [m for m in msgs if m.author_name == "Scheduled wake-up"]
    assert len(wakeup_msgs) == 2

    # Each message records its respective wakeup id
    part_texts = ["".join(p.text for p in m.parts if isinstance(p, TextPart)) for m in wakeup_msgs]
    assert any(pending_before[0].id in t for t in part_texts)
    assert any(pending_before[1].id in t for t in part_texts)

    await session_env.manager.close()
