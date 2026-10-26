from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx2
from sqlalchemy import insert, select

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.messaging.wakeups import (
    WakeupService,
)
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
