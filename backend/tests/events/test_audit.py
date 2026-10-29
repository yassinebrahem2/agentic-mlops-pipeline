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

    async def async_secrets() -> list[str]:
        return [secret]

    audit_log = SqliteAuditLog(databases, secrets=async_secrets)

    ten_thousand_chars = "x" * 5000 + secret + "y" * 5000
    ten_thousand_key = "k" * 10000
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
            f"key_with_{secret}": "key_secret_val",
            ten_thousand_key: "long_key_val",
            "nested": {
                "inner_secret": secret,
                "inner_long": "z" * 6000,
                f"inner_key_{secret}": "inner_val",
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
    # 2. Secret not present anywhere in detail JSON (neither keys nor values)
    assert secret not in json.dumps(record.detail)
    # 3. Secret masked with *** in values
    assert record.detail["secret_val"] == "prefix *** suffix"
    assert record.detail["nested"]["inner_secret"] == "***"
    assert len(record.detail["nested"]["inner_long"]) == 4000
    # 4. Dict keys scrubbed of secret and capped at 4000
    assert "key_with_***" in record.detail
    assert len("k" * 4000) == 4000
    assert ("k" * 4000) in record.detail
    assert "inner_key_***" in record.detail["nested"]
    # 5. URL secret masked
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


@pytest.mark.asyncio
async def test_audit_scrubs_tuples_and_handles_colliding_keys(databases: Databases) -> None:
    project_id = "prj_audit2"
    sec1 = "sec-alpha-111"
    sec2 = "sec-beta-222"

    audit_log = SqliteAuditLog(databases, secrets=[sec1, sec2])

    record = await audit_log.record(
        project_id,
        actor="Orchestrator",
        action="Tool",
        risk_class=RiskClass.READ,
        permission_level="ask",
        decision="allowed",
        target="filesystem",
        detail={
            # Keys that scrub to the exact same text
            f"token_{sec1}": "val1",
            f"token_{sec2}": "val2",
            # Tuple with secrets
            "my_tuple": ("prefix", sec1, ("nested_tuple", sec2)),
            # Colliding keys that exceed 4000 chars
            ("x" * 4500 + "1"): "long_key_1",
            ("x" * 4500 + "2"): "long_key_2",
        },
    )

    detail = record.detail
    # 1. Colliding scrubbed keys kept distinct
    assert "token_***" in detail
    assert "token_***_1" in detail
    assert detail["token_***"] == "val1"
    assert detail["token_***_1"] == "val2"

    # 2. Tuples scrubbed
    assert sec1 not in json.dumps(detail["my_tuple"])
    assert sec2 not in json.dumps(detail["my_tuple"])
    # In JSON deserialization from record.detail, tuple is list or tuple
    assert detail["my_tuple"][0] == "prefix"
    assert detail["my_tuple"][1] == "***"
    assert detail["my_tuple"][2] == ["nested_tuple", "***"] or detail["my_tuple"][2] == (
        "nested_tuple",
        "***",
    )

    # 3. Colliding long keys kept distinct and capped at <= 4000
    long_k1 = "x" * 4000
    long_k2 = "x" * 3998 + "_1"
    assert long_k1 in detail
    assert long_k2 in detail
    assert len(long_k1) == 4000
    assert len(long_k2) == 4000
    assert detail[long_k1] == "long_key_1"
    assert detail[long_k2] == "long_key_2"


@pytest.mark.asyncio
async def test_audit_page_keyset_with_equal_ts(
    databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "prj_auditpage"
    audit_log = SqliteAuditLog(databases, secrets=[])

    # Record 4 records with the exact same timestamp
    same_ts = "2026-10-29T12:00:00.000Z"
    monkeypatch.setattr("mlops_agents.events.audit.now_iso", lambda: same_ts)

    r1 = await audit_log.record(
        project_id,
        actor="ActorA",
        action="A1",
        risk_class=RiskClass.READ,
        permission_level="ask",
        decision="allowed",
        target="t1",
        detail={"k": 1},
    )
    r2 = await audit_log.record(
        project_id,
        actor="ActorB",
        action="A2",
        risk_class=RiskClass.READ,
        permission_level="ask",
        decision="allowed",
        target="t2",
        detail={"k": 2},
    )
    r3 = await audit_log.record(
        project_id,
        actor="ActorA",
        action="A3",
        risk_class=RiskClass.READ,
        permission_level="ask",
        decision="allowed",
        target="t3",
        detail={"k": 3},
    )
    r4 = await audit_log.record(
        project_id,
        actor="ActorA",
        action="A4",
        risk_class=RiskClass.READ,
        permission_level="ask",
        decision="allowed",
        target="t4",
        detail={"k": 4},
    )

    # 1. Filter by actor
    a_records = await audit_log.page(project_id, actor="ActorA")
    assert len(a_records) == 3
    assert all(r.actor == "ActorA" for r in a_records)

    # 2. Keyset pagination on (ts, id) with equal ts
    all_four = [r1, r2, r3, r4]
    # In SQLite, sorted by ts.desc(), id.desc():
    expected_order = sorted(all_four, key=lambda r: (r.ts, r.id), reverse=True)

    page1 = await audit_log.page(project_id, limit=2)
    assert len(page1) == 2
    assert [r.id for r in page1] == [expected_order[0].id, expected_order[1].id]

    cursor = (page1[-1].ts, page1[-1].id)
    page2 = await audit_log.page(project_id, before=cursor, limit=2)
    assert len(page2) == 2
    assert [r.id for r in page2] == [expected_order[2].id, expected_order[3].id]

    # No more records
    cursor2 = (page2[-1].ts, page2[-1].id)
    page3 = await audit_log.page(project_id, before=cursor2, limit=2)
    assert page3 == []
