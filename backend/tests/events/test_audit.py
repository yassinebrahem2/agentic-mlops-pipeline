"""Tests for SqliteAuditLog (I11)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit


@pytest.mark.asyncio
async def test_audit_scrubbing_and_capping(databases: Databases) -> None:
    project_id = "prj_audit1"
    secret = "sk-supersecret-token-12345"
    audit_log = SqliteAuditLog(databases, secrets=[secret])

    ten_thousand_chars = "x" * 5000 + secret + "y" * 5000
    url_with_secret = "https://api.example.com/v1?token=query_secret_val"

    record = await audit_log.record(
        project_id,
        actor="Orchestrator",
        action="Bash",
        risk_class=RiskClass.RUN,
        permission_level="ask",
        decision="allowed",
        target="pytest",
        detail={
            "long_val": ten_thousand_chars,
            "url_val": url_with_secret,
            "secret_val": f"prefix {secret} suffix",
            "nested": {
                "inner_secret": secret,
                "inner_long": "z" * 6000,
            },
            "num": 42,
        },
    )

    assert record.id.startswith("aud_")
    assert record.actor == "Orchestrator"
    assert record.action == "Bash"
    assert record.risk_class == "run"
    assert record.permission_level == "ask"
    assert record.decision == "allowed"
    assert record.target == "pytest"

    # Scrubbed & capped checks
    # 1. Long value length capped at 4000
    assert len(record.detail["long_val"]) == 4000
    # 2. Secret not present in detail
    assert secret not in json.dumps(record.detail)
    # 3. Secret masked with ***
    assert record.detail["secret_val"] == "prefix *** suffix"
    assert record.detail["nested"]["inner_secret"] == "***"
    assert len(record.detail["nested"]["inner_long"]) == 4000
    # 4. URL secret masked
    assert "query_secret_val" not in record.detail["url_val"]
    assert "***" in record.detail["url_val"]

    # Verify persistence in sqlite
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (await conn.execute(select(audit).where(audit.c.id == record.id))).one()
        assert row.id == record.id
        assert row.actor == "Orchestrator"
        assert row.risk_class == "run"
        stored_detail = json.loads(row.detail)
        assert stored_detail == record.detail
        assert secret not in row.detail
