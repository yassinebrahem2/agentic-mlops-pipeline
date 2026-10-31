from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.providers.usage_contract import ModelPrice
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore
from tests.providers.fake_clock import FakeClock

KEY = "sk-or-test-key-123456"
LISTED = {
    "id": "vendor/m1",
    "context_length": 8192,
    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
}
USER = ModelPrice(prompt_per_mtok=1.0, completion_per_mtok=2.0, source="user")


class Listing:
    """The mutable /models answer of a fake OpenRouter."""

    def __init__(self) -> None:
        self.data: list[dict[str, Any]] = [LISTED]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"object": "list", "data": self.data})


@pytest.fixture
def listing() -> Listing:
    return Listing()


@pytest.fixture
async def service(
    databases: Databases, keystore: MemoryKeyStore, listing: Listing
) -> AsyncIterator[ProvidersService]:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(listing.handle))
    svc = ProvidersService(databases, keystore, client, KeyFailover(FakeClock()))
    await svc.create_provider(
        ProviderCreate(name="openrouter", kind="hosted", base_url="https://openrouter.ai/api/v1")
    )
    await svc.add_key("openrouter", KEY)
    yield svc
    await client.aclose()


ADDRESS = "openrouter:vendor/m1"
PROVIDER_PRICE = ModelPrice(prompt_per_mtok=3.0, completion_per_mtok=15.0, source="provider")


async def _add(service: ProvidersService, **extra: object) -> None:
    await service.add_model(
        ModelCreate.model_validate(
            {"provider": "openrouter", "wire_model_id": "vendor/m1", **extra}
        )
    )


async def test_add_stores_the_provider_price(service: ProvidersService) -> None:
    await _add(service)
    assert (await service.require_model(ADDRESS)).price == PROVIDER_PRICE


async def test_add_with_a_given_window_still_stores_the_provider_price(
    service: ProvidersService,
) -> None:
    await _add(service, context_window=4096)
    entry = await service.require_model(ADDRESS)
    assert entry.context_window == 4096
    assert entry.price == PROVIDER_PRICE


async def test_listing_without_pricing_leaves_the_model_unpriced(
    service: ProvidersService, listing: Listing
) -> None:
    listing.data = [{"id": "vendor/m1", "context_length": 8192}]
    await _add(service)
    assert (await service.require_model(ADDRESS)).price is None


async def test_list_remote_models_stores_and_returns_the_provider_price(
    service: ProvidersService, listing: Listing
) -> None:
    listing.data = [{"id": "vendor/m1", "context_length": 8192}]
    await _add(service)
    listing.data = [LISTED]
    remote = await service.list_remote_models("openrouter")
    assert remote[0].price == PROVIDER_PRICE
    assert (await service.require_model(ADDRESS)).price == PROVIDER_PRICE


async def test_a_changed_provider_price_replaces_the_provider_price(
    service: ProvidersService, listing: Listing
) -> None:
    await _add(service)
    listing.data = [{**LISTED, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]
    await service.list_remote_models("openrouter")
    price = (await service.require_model(ADDRESS)).price
    assert price == ModelPrice(prompt_per_mtok=1.0, completion_per_mtok=2.0, source="provider")


async def test_a_listing_that_drops_the_price_keeps_the_stored_one(
    service: ProvidersService, listing: Listing
) -> None:
    await _add(service)
    listing.data = [{"id": "vendor/m1", "context_length": 8192}]
    await service.list_remote_models("openrouter")
    assert (await service.require_model(ADDRESS)).price == PROVIDER_PRICE


async def test_a_user_price_survives_a_later_listing(
    service: ProvidersService, listing: Listing
) -> None:
    await _add(service)
    await service.set_price(ADDRESS, USER)
    await service.list_remote_models("openrouter")
    assert (await service.require_model(ADDRESS)).price == USER


async def test_clearing_the_user_price_lets_the_provider_price_return(
    service: ProvidersService,
) -> None:
    await _add(service)
    await service.set_price(ADDRESS, USER)
    cleared = await service.set_price(ADDRESS, None)
    assert cleared.price is None
    await service.list_remote_models("openrouter")
    assert (await service.require_model(ADDRESS)).price == PROVIDER_PRICE


async def test_set_price_for_an_unknown_model_is_404(service: ProvidersService) -> None:
    with pytest.raises(DomainError) as err:
        await service.set_price("openrouter:nope", USER)
    assert err.value.status == 404
