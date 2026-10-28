import httpx2
import pytest
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.errors import DomainError
from mlops_agents.providers.embedders import EMBEDDER_KEY, EmbeddersService
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.provider_embedder import ProviderEmbedder
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import app_settings, models, project_settings
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


@pytest.fixture
async def providers_svc(
    databases: Databases,
    keystore: MemoryKeyStore,
    model_http: httpx2.AsyncClient,
) -> ProvidersService:
    return ProvidersService(databases, keystore, model_http)


@pytest.fixture
def failover() -> KeyFailover:
    return KeyFailover()


@pytest.fixture
def embedders_svc(
    databases: Databases,
    providers_svc: ProvidersService,
    failover: KeyFailover,
    model_http: httpx2.AsyncClient,
) -> EmbeddersService:
    return EmbeddersService(databases, providers_svc, failover, model_http)


async def _setup_models(
    providers_svc: ProvidersService, fake_openai: RunningFake
) -> tuple[str, str]:
    """Returns (verified_embedder_address, chat_model_address)."""
    await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    embedder = await providers_svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-embed", model_type="embedder")
    )
    await providers_svc.verify_model(embedder.address)

    chat = await providers_svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-tools", model_type="chat")
    )
    await providers_svc.verify_model(chat.address)
    return embedder.address, chat.address


async def test_neither_default_nor_override_configured(
    embedders_svc: EmbeddersService,
) -> None:
    st = await embedders_svc.status("prj_1")
    assert not st.configured
    assert st.address is None
    assert st.dimensions is None
    assert st.problem is None

    def_st = await embedders_svc.default_status()
    assert not def_st.configured
    assert def_st.address is None
    assert def_st.dimensions is None
    assert def_st.problem is None

    embedder = await embedders_svc.for_project("prj_1")
    assert embedder is None


async def test_default_only_and_override_only_and_both(
    embedders_svc: EmbeddersService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
) -> None:
    embed_addr, _ = await _setup_models(providers_svc, fake_openai)
    # Add a second wide embedder model
    wide = await providers_svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-embed-wide", model_type="embedder")
    )
    await providers_svc.verify_model(wide.address)

    # 1. Default only
    await embedders_svc.set_default(embed_addr)
    assert await embedders_svc.chosen_default() == embed_addr
    assert await embedders_svc.chosen_override("prj_1") is None

    st1 = await embedders_svc.status("prj_1")
    assert st1.configured
    assert st1.address == embed_addr
    assert st1.dimensions == 16
    assert st1.problem is None

    emb1 = await embedders_svc.for_project("prj_1")
    assert emb1 is not None
    assert isinstance(emb1, ProviderEmbedder)
    assert emb1.address == embed_addr
    assert emb1.dimensions == 16

    # 2. Both set: override wins
    await embedders_svc.set_override("prj_1", wide.address)
    assert await embedders_svc.chosen_override("prj_1") == wide.address

    st2 = await embedders_svc.status("prj_1")
    assert st2.configured
    assert st2.address == wide.address
    assert st2.dimensions == 32
    assert st2.problem is None

    emb2 = await embedders_svc.for_project("prj_1")
    assert emb2 is not None
    assert emb2.address == wide.address
    assert emb2.dimensions == 32

    # 3. Clear default: override only
    await embedders_svc.set_default(None)
    assert await embedders_svc.chosen_default() is None

    st3 = await embedders_svc.status("prj_1")
    assert st3.configured
    assert st3.address == wide.address
    assert st3.dimensions == 32
    assert st3.problem is None

    emb3 = await embedders_svc.for_project("prj_1")
    assert emb3 is not None
    assert emb3.address == wide.address


async def test_unusable_override_does_not_fall_back_to_default(
    embedders_svc: EmbeddersService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
    databases: Databases,
) -> None:
    embed_addr, _ = await _setup_models(providers_svc, fake_openai)
    # Add second embedder unverified
    unverified = await providers_svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-embed-wide", model_type="embedder")
    )
    # App default is valid
    await embedders_svc.set_default(embed_addr)

    # Force unverified embedder into project override via DB (since set_override rejects it)
    proj_engine = await databases.project("prj_1")
    async with proj_engine.begin() as conn:
        await conn.execute(
            sqlite_insert(project_settings)
            .values(key=EMBEDDER_KEY, value=unverified.address)
            .on_conflict_do_update(
                index_elements=[project_settings.c.key],
                set_={"value": unverified.address},
            )
        )

    # Status shows override is chosen and describes problem
    st = await embedders_svc.status("prj_1")
    assert st.configured
    assert st.address == unverified.address
    assert st.dimensions is None
    assert st.problem is not None
    assert "has not passed verification" in st.problem

    # for_project returns None and DOES NOT fall back to default!
    emb = await embedders_svc.for_project("prj_1")
    assert emb is None


async def test_unusable_models_give_none_and_problem(
    embedders_svc: EmbeddersService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
    databases: Databases,
) -> None:
    _embed_addr, chat_addr = await _setup_models(providers_svc, fake_openai)

    # 1. Chat model set directly in DB
    app_engine = await databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(
            sqlite_insert(app_settings)
            .values(key=EMBEDDER_KEY, value=chat_addr)
            .on_conflict_do_update(
                index_elements=[app_settings.c.key],
                set_={"value": chat_addr},
            )
        )

    st_chat = await embedders_svc.default_status()
    assert st_chat.configured
    assert st_chat.address == chat_addr
    assert st_chat.problem is not None
    assert "is a chat model, not an embedder model. Choose an embedder model." in st_chat.problem
    assert await embedders_svc.for_project("prj_1") is None

    # 2. Deleted model set directly in DB
    async with app_engine.begin() as conn:
        await conn.execute(
            sqlite_insert(app_settings)
            .values(key=EMBEDDER_KEY, value="fake:deleted-model")
            .on_conflict_do_update(
                index_elements=[app_settings.c.key],
                set_={"value": "fake:deleted-model"},
            )
        )

    st_del = await embedders_svc.default_status()
    assert st_del.configured
    assert st_del.address == "fake:deleted-model"
    assert st_del.problem is not None
    assert "no longer exists" in st_del.problem
    assert await embedders_svc.for_project("prj_1") is None


async def test_set_default_and_set_override_validation(
    embedders_svc: EmbeddersService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
) -> None:
    embed_addr, chat_addr = await _setup_models(providers_svc, fake_openai)
    unverified = await providers_svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-embed-wide", model_type="embedder")
    )

    # Unknown model -> 404 model_not_found
    with pytest.raises(DomainError) as err:
        await embedders_svc.set_default("fake:unknown")
    assert err.value.status == 404
    assert err.value.code == "model_not_found"

    with pytest.raises(DomainError) as err:
        await embedders_svc.set_override("prj_1", "fake:unknown")
    assert err.value.status == 404
    assert err.value.code == "model_not_found"

    # Chat model -> 409 model_not_usable
    with pytest.raises(DomainError) as err:
        await embedders_svc.set_default(chat_addr)
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    assert "is a chat model, not an embedder model. Choose an embedder model." in err.value.message

    with pytest.raises(DomainError) as err:
        await embedders_svc.set_override("prj_1", chat_addr)
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    assert "is a chat model, not an embedder model. Choose an embedder model." in err.value.message

    # Unverified embedder -> 409 model_not_usable
    with pytest.raises(DomainError) as err:
        await embedders_svc.set_default(unverified.address)
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    assert "has not passed verification" in err.value.message

    # Clearing works
    await embedders_svc.set_default(embed_addr)
    assert await embedders_svc.chosen_default() == embed_addr
    await embedders_svc.set_default(None)
    assert await embedders_svc.chosen_default() is None


async def test_two_projects_have_independent_overrides(
    embedders_svc: EmbeddersService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
) -> None:
    embed_addr, _ = await _setup_models(providers_svc, fake_openai)
    wide = await providers_svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-embed-wide", model_type="embedder")
    )
    await providers_svc.verify_model(wide.address)

    await embedders_svc.set_override("prj_1", embed_addr)
    await embedders_svc.set_override("prj_2", wide.address)

    assert await embedders_svc.chosen_override("prj_1") == embed_addr
    assert await embedders_svc.chosen_override("prj_2") == wide.address

    st1 = await embedders_svc.status("prj_1")
    st2 = await embedders_svc.status("prj_2")
    assert st1.dimensions == 16
    assert st2.dimensions == 32

    # Clearing prj_1 doesn't affect prj_2
    await embedders_svc.set_override("prj_1", None)
    assert await embedders_svc.chosen_override("prj_1") is None
    assert await embedders_svc.chosen_override("prj_2") == wide.address


async def test_provider_marked_needs_attention_is_still_usable(
    embedders_svc: EmbeddersService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
) -> None:
    embed_addr, _ = await _setup_models(providers_svc, fake_openai)
    provider = await providers_svc.get_provider("fake")
    await providers_svc.mark_needs_attention(provider.id, "exhausted keys")
    await embedders_svc.set_default(embed_addr)

    st = await embedders_svc.default_status()
    assert st.configured
    assert st.problem is None
    assert st.dimensions == 16

    emb = await embedders_svc.for_project("prj_1")
    assert emb is not None
    assert emb.address == embed_addr


async def test_hosted_provider_with_no_key_is_unusable(
    embedders_svc: EmbeddersService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
    databases: Databases,
) -> None:
    await providers_svc.create_provider(
        ProviderCreate(name="hosted-nokey", kind="hosted", base_url=fake_openai.base_url)
    )
    m = await providers_svc.add_model(
        ModelCreate(provider="hosted-nokey", wire_model_id="fake-embed", model_type="embedder")
    )
    # Mark verified so verification check passes and "has no key" check is reached
    app_engine = await databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(
            models.update()
            .where(models.c.address == m.address)
            .values(availability="verified", dimensions=16)
        )
    with pytest.raises(DomainError) as err:
        await embedders_svc.set_default(m.address)
    assert err.value.status == 409
    assert "has no key" in err.value.message

    # Force into app_settings to test status and for_project
    async with app_engine.begin() as conn:
        await conn.execute(
            sqlite_insert(app_settings)
            .values(key=EMBEDDER_KEY, value=m.address)
            .on_conflict_do_update(
                index_elements=[app_settings.c.key],
                set_={"value": m.address},
            )
        )
    st = await embedders_svc.default_status()
    assert st.configured
    assert st.problem is not None
    assert "has no key" in st.problem
    assert await embedders_svc.for_project("prj_1") is None


async def test_provider_gone_gives_problem_and_none(
    embedders_svc: EmbeddersService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embed_addr, _ = await _setup_models(providers_svc, fake_openai)
    provider = await providers_svc.get_provider("fake")
    await embedders_svc.set_default(embed_addr)

    # Simulate provider row being absent when get_provider_by_id is called
    orig_get = providers_svc.get_provider_by_id

    async def get_missing(pid: str):
        if pid == provider.id:
            raise DomainError("provider_not_found", "Provider not found", status=404)
        return await orig_get(pid)

    monkeypatch.setattr(providers_svc, "get_provider_by_id", get_missing)

    st = await embedders_svc.default_status()
    assert st.configured
    assert st.problem is not None
    assert "no longer exists" in st.problem
    assert await embedders_svc.for_project("prj_1") is None

    with pytest.raises(DomainError) as err:
        await embedders_svc.set_override("prj_1", embed_addr)
    assert err.value.status == 409
    assert "no longer exists" in err.value.message
