"""Heartbeat health check route (unauthenticated)."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.monitoring.scheduler.contract import HeartbeatView

router = APIRouter(tags=["health"])


@router.get("/health/heartbeat", response_model=HeartbeatView)
async def heartbeat() -> HeartbeatView:
    return HeartbeatView(ok=True)
