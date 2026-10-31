from __future__ import annotations

from typing import Any

import httpx2
from fastapi import FastAPI
from sqlalchemy import insert

from mlops_agents.clock import now_iso
from mlops_agents.storage.tables import projects
from tests.fakes.fake_openai import RunningFake
from tests.providers.usage_seed import seed_calls

MONDAY = "2026-10-26T00:00:00.000Z"
SUNDAY_BEFORE = "2026-10-25T23:59:59.000Z"


async def _add_project(app: FastAPI, project_id: str) -> None:
    engine = await app.state.services.databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=project_id,
                name=project_id,
                source="github",
                github_full_name=f"org/{project_id}",
                github_key=f"org/{project_id}",
                default_branch="main",
                status="ok",
                created_at=now_iso(),
            )
        )


async def _add_model(client: httpx2.AsyncClient, fake: RunningFake, wire: str) -> dict[str, Any]:
    res = await client.post(
        "/api/providers",
        json={"name": "fake", "kind": "self_hosted", "base_url": fake.base_url},
    )
    assert res.status_code in (201, 409), res.text
    res = await client.post("/api/models", json={"provider": "fake", "wire_model_id": wire})
    assert res.status_code == 201, res.text
    return res.json()


async def test_anonymous_cannot_access_usage_or_price(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/usage")).status_code == 401
    assert (await anonymous.put("/api/models/price", json={})).status_code == 401


async def test_usage_sums_two_projects_with_and_without_a_price(
    client: httpx2.AsyncClient, app: FastAPI, fake_openai: RunningFake
) -> None:
    await _add_model(client, fake_openai, "fake-tools")
    await _add_project(app, "prj_one")
    await _add_project(app, "prj_two")
    dbs = app.state.services.databases
    await seed_calls(
        dbs,
        "prj_one",
        [
            ("orchestrator", "fake:fake-tools", 1_000_000, 500_000, MONDAY),
            ("orchestrator", "fake:fake-tools", None, 100, SUNDAY_BEFORE),
            ("orchestrator", "fake:gone", 10, 10, MONDAY),
        ],
    )
    await seed_calls(dbs, "prj_two", [("auditor", "fake:fake-tools", 1_000_000, 0, MONDAY)])

    res = await client.put(
        "/api/models/price",
        json={
            "address": "fake:fake-tools",
            "price": {"prompt_per_mtok": 2, "completion_per_mtok": 4},
        },
    )
    assert res.status_code == 200, res.text

    body = (await client.get("/api/usage")).json()
    rows = {(r["project_id"], r["model_address"], r["week_start"]): r for r in body["rows"]}
    assert rows[("prj_one", "fake:fake-tools", "2026-10-26")]["cost_usd"] == 4.0
    assert rows[("prj_one", "fake:fake-tools", "2026-10-19")]["completion_tokens"] == 100
    assert rows[("prj_one", "fake:fake-tools", "2026-10-19")]["prompt_tokens"] == 0
    assert rows[("prj_two", "fake:fake-tools", "2026-10-26")]["cost_usd"] == 2.0
    assert rows[("prj_one", "fake:gone", "2026-10-26")]["cost_usd"] is None
    assert body["unpriced_models"] == ["fake:gone"]


async def test_usage_since_filters_by_date(client: httpx2.AsyncClient, app: FastAPI) -> None:
    await _add_project(app, "prj_one")
    await seed_calls(
        app.state.services.databases,
        "prj_one",
        [("a", "fake:m", 1, 1, SUNDAY_BEFORE), ("a", "fake:m", 10, 10, MONDAY)],
    )
    body = (await client.get("/api/usage", params={"since": "2026-10-26"})).json()
    assert [r["prompt_tokens"] for r in body["rows"]] == [10]
    assert (await client.get("/api/usage", params={"since": "not-a-date"})).status_code == 422


async def test_usage_without_projects_is_empty(client: httpx2.AsyncClient) -> None:
    res = await client.get("/api/usage")
    assert res.json() == {"rows": [], "unpriced_models": []}


async def test_price_route_sets_clears_and_marks_the_user(
    client: httpx2.AsyncClient, fake_openai: RunningFake
) -> None:
    await _add_model(client, fake_openai, "fake-tools")
    set_body = {
        "address": "fake:fake-tools",
        "price": {"prompt_per_mtok": 1.5, "completion_per_mtok": 0},
    }
    res = await client.put("/api/models/price", json=set_body)
    assert res.status_code == 200, res.text
    assert res.json()["price"] == {
        "prompt_per_mtok": 1.5,
        "completion_per_mtok": 0.0,
        "source": "user",
    }
    res = await client.put("/api/models/price", json={"address": "fake:fake-tools", "price": None})
    assert res.status_code == 200
    assert res.json()["price"] is None


async def test_price_route_unknown_model_is_404(client: httpx2.AsyncClient) -> None:
    res = await client.put("/api/models/price", json={"address": "fake:nope", "price": None})
    assert res.status_code == 404
    assert res.json()["code"] == "model_not_found"


async def test_price_route_takes_an_address_with_a_slash(
    client: httpx2.AsyncClient, fake_openai: RunningFake
) -> None:
    await _add_model(client, fake_openai, "vendor/slashed")
    res = await client.put(
        "/api/models/price",
        json={
            "address": "fake:vendor/slashed",
            "price": {"prompt_per_mtok": 1, "completion_per_mtok": 2},
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["price"]["prompt_per_mtok"] == 1.0


async def test_price_route_refuses_bad_bodies(
    client: httpx2.AsyncClient, fake_openai: RunningFake
) -> None:
    await _add_model(client, fake_openai, "fake-tools")
    address = "fake:fake-tools"
    bad_prices: list[dict[str, object]] = [
        {"prompt_per_mtok": 1, "completion_per_mtok": 2, "source": "provider"},
        {"prompt_per_mtok": -1, "completion_per_mtok": 2},
        {"prompt_per_mtok": 1, "completion_per_mtok": -0.01},
        {"prompt_per_mtok": 1},
    ]
    for price in bad_prices:
        res = await client.put("/api/models/price", json={"address": address, "price": price})
        assert res.status_code == 422, price
    for raw in (
        '{"address": "fake:fake-tools", "source": "user", "price": null}',
        '{"address": "fake:fake-tools", "price": {"prompt_per_mtok": NaN, "completion_per_mtok": 1}}',
        '{"address": "fake:fake-tools", "price": {"prompt_per_mtok": Infinity, "completion_per_mtok": 1}}',
    ):
        res = await client.put(
            "/api/models/price", content=raw, headers={"content-type": "application/json"}
        )
        assert res.status_code == 422, raw
    assert (await client.get("/api/models")).json()[0]["price"] is None


async def test_verify_stores_a_published_price_but_not_over_a_user_price(
    client: httpx2.AsyncClient, fake_openai: RunningFake
) -> None:
    await _add_model(client, fake_openai, "fake-tools")
    fake_openai.state.pricing["fake-tools"] = {"prompt": "0.000003", "completion": "0.000015"}
    res = await client.post("/api/models/verify", json={"address": "fake:fake-tools"})
    assert res.json()["price"] == {
        "prompt_per_mtok": 3.0,
        "completion_per_mtok": 15.0,
        "source": "provider",
    }

    user = {"prompt_per_mtok": 9, "completion_per_mtok": 9}
    await client.put("/api/models/price", json={"address": "fake:fake-tools", "price": user})
    res = await client.post("/api/models/verify", json={"address": "fake:fake-tools"})
    assert res.json()["price"]["source"] == "user"
    assert res.json()["price"]["prompt_per_mtok"] == 9.0
