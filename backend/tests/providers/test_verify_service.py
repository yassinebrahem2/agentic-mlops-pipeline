import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.providers.fake_clock import FakeClock


async def _service(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    http: httpx2.AsyncClient,
) -> ProvidersService:
    failover = KeyFailover(FakeClock())
    svc = ProvidersService(databases, keystore, http, failover)
    await svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    return svc


async def test_verify_records_traits(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = await _service(databases, keystore, fake_openai, model_http)
    entry = await svc.add_model(ModelCreate(provider="fake", wire_model_id="fake-tools"))
    assert entry.context_window == 32768
    verified = await svc.verify_model("fake:fake-tools")
    assert verified.availability == "verified"
    assert verified.tool_calling
    assert verified.streaming
    assert verified.verified_at is not None


async def test_failed_verification_marks_the_model(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = await _service(databases, keystore, fake_openai, model_http)
    await svc.add_model(ModelCreate(provider="fake", wire_model_id="fake-tools"))
    fake_openai.state.auth_fail = True
    with pytest.raises(DomainError) as err:
        await svc.verify_model("fake:fake-tools")
    assert err.value.code == "verification_failed"
    entry = await svc.require_model("fake:fake-tools")
    assert entry.availability == "needs_attention"
