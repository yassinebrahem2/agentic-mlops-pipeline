from __future__ import annotations

from datetime import date

import pytest

from mlops_agents.providers.usage import cost_usd, provider_price, usage_view
from mlops_agents.providers.usage_contract import ModelPrice
from mlops_agents.storage.databases import Databases
from tests.providers.usage_seed import seed_calls

PRICE = ModelPrice(prompt_per_mtok=3.0, completion_per_mtok=15.0, source="user")

# Monday 2026-10-26 is the start of a week; the call just before it belongs to the week before.
SUNDAY_LAST_SECOND = "2026-10-25T23:59:59.999Z"
MONDAY_FIRST_SECOND = "2026-10-26T00:00:00.000Z"


def test_per_token_string_becomes_per_million() -> None:
    price = provider_price("0.000003", "0.000015")
    assert price == ModelPrice(prompt_per_mtok=3.0, completion_per_mtok=15.0, source="provider")


@pytest.mark.parametrize(
    ("prompt", "completion"),
    [
        (None, "0.1"),
        ("0.1", None),
        ("abc", "0.1"),
        ("0.1", "-0.5"),
        ("-1", "0.1"),
        ("nan", "0.1"),
        ("inf", "0.1"),
        (True, "0.1"),
        (3, "0.1"),
    ],
)
def test_unusable_provider_price_is_none(prompt: object, completion: object) -> None:
    assert provider_price(prompt, completion) is None


def test_free_model_price_is_zero_not_missing() -> None:
    price = provider_price("0", "0")
    assert price is not None
    assert price.prompt_per_mtok == 0.0


def test_cost_usd() -> None:
    assert cost_usd(1_000_000, 2_000_000, PRICE) == 33.0
    assert cost_usd(0, 0, PRICE) == 0.0
    assert cost_usd(10, 10, None) is None


async def test_usage_sums_by_project_agent_model_and_week(databases: Databases) -> None:
    await seed_calls(
        databases,
        "prj_one",
        [
            ("orchestrator", "groq:a", 100, 10, "2026-10-27T10:00:00.000Z"),
            ("orchestrator", "groq:a", 50, None, "2026-10-28T10:00:00.000Z"),
            ("orchestrator", "groq:a", None, 5, "2026-10-28T11:00:00.000Z"),
            ("auditor", "groq:a", 7, 1, "2026-10-28T11:00:00.000Z"),
            ("orchestrator", "groq:b", 1_000_000, 0, "2026-10-28T11:00:00.000Z"),
        ],
    )
    await seed_calls(
        databases, "prj_two", [("orchestrator", "groq:a", 1, 2, "2026-10-29T10:00:00.000Z")]
    )

    view = await usage_view(databases, ["prj_one", "prj_two"], {"groq:a": PRICE}, since=None)

    got = {
        (r.project_id, r.agent, r.model_address, r.week_start): (
            r.prompt_tokens,
            r.completion_tokens,
        )
        for r in view.rows
    }
    week = date(2026, 10, 26)
    assert got == {
        ("prj_one", "orchestrator", "groq:a", week): (150, 15),
        ("prj_one", "auditor", "groq:a", week): (7, 1),
        ("prj_one", "orchestrator", "groq:b", week): (1_000_000, 0),
        ("prj_two", "orchestrator", "groq:a", week): (1, 2),
    }


async def test_calls_either_side_of_monday_midnight_utc_land_in_two_weeks(
    databases: Databases,
) -> None:
    await seed_calls(
        databases,
        "prj_one",
        [
            ("orchestrator", "groq:a", 1, 1, SUNDAY_LAST_SECOND),
            ("orchestrator", "groq:a", 10, 10, MONDAY_FIRST_SECOND),
        ],
    )
    view = await usage_view(databases, ["prj_one"], {}, since=None)
    weeks = {r.week_start: r.prompt_tokens for r in view.rows}
    assert weeks == {date(2026, 10, 19): 1, date(2026, 10, 26): 10}


async def test_cost_with_price_none_without_and_unpriced_models_listed(
    databases: Databases,
) -> None:
    await seed_calls(
        databases,
        "prj_one",
        [
            ("orchestrator", "groq:priced", 1_000_000, 1_000_000, MONDAY_FIRST_SECOND),
            ("orchestrator", "groq:free-form", 5, 5, MONDAY_FIRST_SECOND),
            ("orchestrator", "groq:deleted", 5, 5, MONDAY_FIRST_SECOND),
        ],
    )
    view = await usage_view(databases, ["prj_one"], {"groq:priced": PRICE}, since=None)
    costs = {r.model_address: r.cost_usd for r in view.rows}
    assert costs == {"groq:priced": 18.0, "groq:free-form": None, "groq:deleted": None}
    assert view.unpriced_models == ["groq:deleted", "groq:free-form"]


async def test_since_keeps_calls_from_that_day_on(databases: Databases) -> None:
    await seed_calls(
        databases,
        "prj_one",
        [
            ("orchestrator", "groq:a", 1, 1, "2026-10-27T23:59:59.000Z"),
            ("orchestrator", "groq:a", 10, 10, "2026-10-28T00:00:00.000Z"),
        ],
    )
    view = await usage_view(databases, ["prj_one"], {}, since=date(2026, 10, 28))
    assert [r.prompt_tokens for r in view.rows] == [10]


async def test_no_calls_is_an_empty_view(databases: Databases) -> None:
    view = await usage_view(databases, ["prj_one"], {}, since=None)
    assert view.rows == []
    assert view.unpriced_models == []
