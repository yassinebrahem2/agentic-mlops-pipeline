from pathlib import Path
from typing import NoReturn

import httpx2
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.settings import (
    AgentLimitsUpdate,
    AgentSettingsService,
)
from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.errors import DomainError
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import agent_model_defaults
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

SECRET_KEY_1 = "gsk_test_key_alpha_111111"
SECRET_KEY_2 = "gsk_test_key_beta_222222"
SECRET_KEY_3 = "gsk_test_key_gamma_333333"
TOOLS = STAGE_2_TOOLS


@pytest.fixture
def catalog(tmp_path: Path) -> AgentCatalog:
    extra = tmp_path / "extra_definitions"
    extra.mkdir(parents=True, exist_ok=True)
    (extra / "helper.md").write_text(
        """---
name: helper
display_name: Helper
description: Helps the orchestrator.
tools: [Read]
limits:
  max_steps: 10
---
You help.
""",
        encoding="utf-8",
    )
    return AgentCatalog.load(TOOLS, extra_dirs=[extra])


@pytest.fixture
async def providers_svc(
    databases: Databases,
    keystore: MemoryKeyStore,
    model_http: httpx2.AsyncClient,
) -> ProvidersService:
    return ProvidersService(databases, keystore, model_http)


@pytest.fixture
def settings_svc(
    databases: Databases,
    providers_svc: ProvidersService,
    catalog: AgentCatalog,
) -> AgentSettingsService:
    return AgentSettingsService(databases, providers_svc, catalog)


async def test_unknown_agent_rejected(settings_svc: AgentSettingsService) -> None:
    calls = [
        lambda: settings_svc.get("unknown"),
        lambda: settings_svc.get("unknown", "prj_1"),
        lambda: settings_svc.set_default_model("unknown", None),
        lambda: settings_svc.set_project_model("prj_1", "unknown", None),
        lambda: settings_svc.set_project_limits(
            "prj_1", "unknown", AgentLimitsUpdate(max_steps=None)
        ),
        lambda: settings_svc.resolve("prj_1", "unknown"),
    ]
    for fn in calls:
        with pytest.raises(DomainError) as err:
            await fn()
        assert err.value.status == 404
        assert err.value.code == "agent_not_found"
        assert err.value.message == "There is no agent named unknown."


async def test_app_default_lifecycle_and_validation(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
) -> None:
    # 1. Initially no default model
    view = await settings_svc.get("orchestrator")
    assert view.agent == "orchestrator"
    assert view.display_name == "Orchestrator"
    assert view.default_model is None
    assert view.project_model is None
    assert view.effective_model is None
    assert view.model_problem == (
        "Choose a model for Orchestrator in AI providers, or in this project's settings."
    )
    assert view.max_steps == 25

    # 2. Unknown model rejected with 404 model_not_found
    with pytest.raises(DomainError) as err:
        await settings_svc.set_default_model("orchestrator", "groq:nonexistent")
    assert err.value.status == 404
    assert err.value.code == "model_not_found"

    # Set up a provider
    base_url = fake_openai.base_url
    await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)

    # 3. Untested model rejected with 409 model_not_usable
    untested = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    with pytest.raises(DomainError) as err:
        await settings_svc.set_default_model("orchestrator", untested.address)
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    assert "has not passed verification" in err.value.message

    # 4. Verified model without a context window rejected naming context window
    # Add wire id not listed by fake server so context_window is None
    no_ctx = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="unknown-wire-model")
    )
    assert no_ctx.context_window is None
    await providers_svc.record_verification(
        no_ctx.address, tool_calling=True, streaming=True, detail=None
    )
    with pytest.raises(DomainError) as err:
        await settings_svc.set_default_model("orchestrator", no_ctx.address)
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    assert err.value.message == (
        f"The context window of {no_ctx.address} is unknown. Enter it in AI providers."
    )

    # 5. Verify the usable model and set it as app default
    await providers_svc.verify_model(untested.address)
    view = await settings_svc.set_default_model("orchestrator", untested.address)
    assert view.default_model == untested.address
    assert view.effective_model == untested.address
    assert view.project_model is None
    assert view.model_problem is None

    # Read back via get
    view_get = await settings_svc.get("orchestrator")
    assert view_get == view

    # Read back via list_settings (includes helper, orchestrator, and all packaged agents)
    listing = await settings_svc.list_settings()
    helper = next(v for v in listing if v.agent == "helper")
    orch = next(v for v in listing if v.agent == "orchestrator")
    assert helper.display_name == "Helper"
    assert helper.default_model is None
    assert orch.display_name == "Orchestrator"
    assert orch.default_model == untested.address

    # 6. Clear with None
    cleared = await settings_svc.set_default_model("orchestrator", None)
    assert cleared.default_model is None
    assert cleared.effective_model is None
    assert cleared.model_problem == (
        "Choose a model for Orchestrator in AI providers, or in this project's settings."
    )


async def test_project_model_override(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    databases: Databases,
    fake_openai: RunningFake,
) -> None:
    base_url = fake_openai.base_url
    await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)

    model_a = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(model_a.address)

    await providers_svc.create_provider(
        ProviderCreate(name="gemini", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("gemini", SECRET_KEY_2)
    model_b = await providers_svc.add_model(
        ModelCreate(provider="gemini", wire_model_id="fake-tools", context_window=16384)
    )
    await providers_svc.verify_model(model_b.address)

    # Set app default to model A
    await settings_svc.set_default_model("orchestrator", model_a.address)

    # Ensure project database exists
    await databases.project("prj_1")
    await databases.project("prj_2")

    # Set project 1 override to model B
    p1_view = await settings_svc.set_project_model("prj_1", "orchestrator", model_b.address)
    assert p1_view.default_model == model_a.address
    assert p1_view.project_model == model_b.address
    assert p1_view.effective_model == model_b.address

    # Project 2 still sees default model A
    p2_view = await settings_svc.get("orchestrator", "prj_2")
    assert p2_view.default_model == model_a.address
    assert p2_view.project_model is None
    assert p2_view.effective_model == model_a.address

    # View outside project sees default model A
    app_view = await settings_svc.get("orchestrator")
    assert app_view.default_model == model_a.address
    assert app_view.project_model is None
    assert app_view.effective_model == model_a.address

    # Clear project 1 override with None -> falls back to default A
    cleared_p1 = await settings_svc.set_project_model("prj_1", "orchestrator", None)
    assert cleared_p1.default_model == model_a.address
    assert cleared_p1.project_model is None
    assert cleared_p1.effective_model == model_a.address

    # Project override works with no app default
    await settings_svc.set_default_model("orchestrator", None)
    p1_view_no_default = await settings_svc.set_project_model(
        "prj_1", "orchestrator", model_b.address
    )
    assert p1_view_no_default.default_model is None
    assert p1_view_no_default.project_model == model_b.address
    assert p1_view_no_default.effective_model == model_b.address


async def test_limits_and_validation(
    settings_svc: AgentSettingsService,
    databases: Databases,
) -> None:
    # 1. Validation fails when max_steps is missing (no default), 0, or negative
    with pytest.raises(ValidationError):
        AgentLimitsUpdate.model_validate({})
    with pytest.raises(ValidationError):
        AgentLimitsUpdate(max_steps=0)
    with pytest.raises(ValidationError):
        AgentLimitsUpdate(max_steps=-1)

    await databases.project("prj_1")
    await databases.project("prj_2")

    # 2. Default limits from agent definition
    view = await settings_svc.get("orchestrator", "prj_1")
    assert view.max_steps == 25
    helper_view = await settings_svc.get("helper", "prj_1")
    assert helper_view.max_steps == 10

    # 3. Project override of 5 wins
    updated = await settings_svc.set_project_limits(
        "prj_1", "orchestrator", AgentLimitsUpdate(max_steps=5)
    )
    assert updated.max_steps == 5
    assert (await settings_svc.get("orchestrator", "prj_1")).max_steps == 5

    # Project 2 still has default
    assert (await settings_svc.get("orchestrator", "prj_2")).max_steps == 25

    # 4. None means no limit and is kept as an override (not reverted to 25)
    no_limit = await settings_svc.set_project_limits(
        "prj_1", "orchestrator", AgentLimitsUpdate(max_steps=None)
    )
    assert no_limit.max_steps is None
    read_back = await settings_svc.get("orchestrator", "prj_1")
    assert read_back.max_steps is None


async def test_resolve_and_problem_states(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
) -> None:
    base_url = fake_openai.base_url
    await databases.project("prj_1")

    # 1. Model unassigned
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "model_unassigned"
    assert err.value.message == (
        "Choose a model for Orchestrator in AI providers, or in this project's settings."
    )
    get_view = await settings_svc.get("orchestrator", "prj_1")
    assert get_view.model_problem == err.value.message

    # Setup hosted provider with 2 keys in position order
    provider = await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)
    await providers_svc.add_key("groq", SECRET_KEY_2)

    model = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(model.address)
    await settings_svc.set_default_model("orchestrator", model.address)

    # 2. Normal resolve returns model with keys in position order and max_steps
    resolved, max_steps = await settings_svc.resolve("prj_1", "orchestrator")
    assert resolved.address == model.address
    assert resolved.provider_id == provider.id
    assert resolved.provider_name == "groq"
    assert resolved.base_url == base_url
    assert resolved.wire_model_id == "fake-tools"
    assert resolved.context_window == 32768
    assert resolved.keys == (SECRET_KEY_1, SECRET_KEY_2)
    assert max_steps == 25
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem is None

    # 3. Self-hosted provider without a key resolves fine
    await providers_svc.create_provider(
        ProviderCreate(name="ollama", kind="self_hosted", base_url=base_url)
    )
    sh_model = await providers_svc.add_model(
        ModelCreate(provider="ollama", wire_model_id="fake-tools", context_window=8192)
    )
    await providers_svc.verify_model(sh_model.address)
    await settings_svc.set_project_model("prj_1", "orchestrator", sh_model.address)

    sh_resolved, _ = await settings_svc.resolve("prj_1", "orchestrator")
    assert sh_resolved.address == sh_model.address
    assert sh_resolved.keys == ()
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem is None

    # Switch back to groq default
    await settings_svc.set_project_model("prj_1", "orchestrator", None)

    # 4. Model problem: mark_model_problem on the model
    await providers_svc.mark_model_problem(model.address, "Quota exhausted mid-turn")
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    expected_msg = (
        f"The model {model.address} failed: Quota exhausted mid-turn. "
        "Verify it again in AI providers or choose another model."
    )
    assert err.value.message == expected_msg
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem == expected_msg

    # Re-verify model to restore
    await providers_svc.verify_model(model.address)
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem is None

    # 5. Provider needs attention
    await providers_svc.mark_needs_attention(provider.id, "Account billing suspended")
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "provider_needs_attention"
    expected_msg = "groq needs attention: Account billing suspended. Fix it in AI providers."
    assert err.value.message == expected_msg
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem == expected_msg

    # Adding a working key proves the key works and automatically resets status to ok
    await providers_svc.add_key("groq", SECRET_KEY_3)
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem is None

    # 6. Hosted provider with no keys (remove all keys)
    prov = await providers_svc.get_provider("groq")
    for k in prov.keys:
        await providers_svc.remove_key("groq", k.position)
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "provider_has_no_key"
    assert err.value.message == "groq has no key. Add one in AI providers."
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem == (
        "groq has no key. Add one in AI providers."
    )

    # 7. Keystore value missing behind a hint:
    # Add a key back, then delete its secret from keystore directly
    await providers_svc.add_key("groq", SECRET_KEY_1)
    # The hint row exists in SQLite, but delete keystore ref
    for ref in list(keystore.data.keys()):
        keystore.delete(ref)

    # get() still sees hint in DB -> model_problem is None
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem is None

    # But resolve() calls keys_for() -> empty -> raises provider_has_no_key
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "provider_has_no_key"
    assert err.value.message == "groq has no key. Add one in AI providers."

    # Restore key
    keystore.data.clear()
    current_prov = await providers_svc.get_provider("groq")
    await providers_svc.remove_key("groq", current_prov.keys[0].position)
    await providers_svc.add_key("groq", SECRET_KEY_1)

    # 8. Model deleted -> model_missing
    await providers_svc.delete_model(model.address)
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "model_missing"
    expected_msg = (
        f"The model {model.address} no longer exists. Choose another model for Orchestrator."
    )
    assert err.value.message == expected_msg
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem == expected_msg

    # 9. Provider deleted -> model_missing
    new_model = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(new_model.address)
    await settings_svc.set_default_model("orchestrator", new_model.address)
    await providers_svc.delete_provider("groq")

    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "model_missing"
    expected_msg = (
        f"The model {new_model.address} no longer exists. Choose another model for Orchestrator."
    )
    assert err.value.message == expected_msg
    assert (await settings_svc.get("orchestrator", "prj_1")).model_problem == expected_msg


class BrokenKeyStore(MemoryKeyStore):
    def __init__(self) -> None:
        super().__init__()
        self.locked = False

    def get(self, ref: str) -> str | None:
        if self.locked:
            raise KeyStoreError("Keyring is locked")
        return super().get(ref)


async def test_list_settings_works_with_locked_keystore(
    databases: Databases,
    fake_openai: RunningFake,
    catalog: AgentCatalog,
) -> None:
    broken_keystore = BrokenKeyStore()
    client = httpx2.AsyncClient()
    providers_svc = ProvidersService(databases, broken_keystore, client)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)

    try:
        # Create a hosted provider with a key and verified model as orchestrator default
        await providers_svc.create_provider(
            ProviderCreate(name="groq", kind="hosted", base_url=fake_openai.base_url)
        )
        await providers_svc.add_key("groq", SECRET_KEY_1)
        model = await providers_svc.add_model(
            ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
        )
        await providers_svc.verify_model(model.address)
        await settings_svc.set_default_model("orchestrator", model.address)

        # Now lock the keystore
        broken_keystore.locked = True

        # list_settings must not touch keystore.get, so it succeeds even when keyring is locked
        listing = await settings_svc.list_settings()
        assert len(listing) == len(catalog.all())
        orch = next(v for v in listing if v.agent == "orchestrator")
        assert orch.effective_model == model.address
        assert orch.model_problem is None
    finally:
        await client.aclose()


async def test_no_secrets_in_views_or_messages(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    databases: Databases,
    fake_openai: RunningFake,
) -> None:
    base_url = fake_openai.base_url
    await databases.project("prj_1")
    await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)
    model = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(model.address)
    await settings_svc.set_default_model("orchestrator", model.address)

    # Views
    view = await settings_svc.get("orchestrator", "prj_1")
    assert SECRET_KEY_1 not in view.model_dump_json()
    listing = await settings_svc.list_settings("prj_1")
    for v in listing:
        assert SECRET_KEY_1 not in v.model_dump_json()

    # Model problem message
    await providers_svc.mark_model_problem(model.address, "problem detail")
    view_with_problem = await settings_svc.get("orchestrator", "prj_1")
    assert SECRET_KEY_1 not in str(view_with_problem.model_problem)
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert SECRET_KEY_1 not in err.value.message


async def test_setters_accept_verified_model_without_provider_keys(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    databases: Databases,
    fake_openai: RunningFake,
) -> None:
    base_url = fake_openai.base_url
    await databases.project("prj_1")

    await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)
    model = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(model.address)

    # Remove the key so the hosted provider has no keys saved
    prov = await providers_svc.get_provider("groq")
    await providers_svc.remove_key("groq", prov.keys[0].position)
    prov_no_keys = await providers_svc.get_provider("groq")
    assert not prov_no_keys.keys

    # set_default_model succeeds even though provider has no keys
    view = await settings_svc.set_default_model("orchestrator", model.address)
    assert view.default_model == model.address
    assert view.effective_model == model.address
    assert view.model_problem == "groq has no key. Add one in AI providers."

    # resolve raises provider_has_no_key
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "provider_has_no_key"
    assert err.value.message == "groq has no key. Add one in AI providers."

    # Clear default
    await settings_svc.set_default_model("orchestrator", None)

    # set_project_model also succeeds without keys
    p_view = await settings_svc.set_project_model("prj_1", "orchestrator", model.address)
    assert p_view.project_model == model.address
    assert p_view.effective_model == model.address
    assert p_view.model_problem == "groq has no key. Add one in AI providers."

    # resolve still raises provider_has_no_key
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "provider_has_no_key"


async def test_check_model_handles_provider_not_found(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    databases: Databases,
    fake_openai: RunningFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = fake_openai.base_url
    await databases.project("prj_1")
    await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)
    model = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(model.address)
    await settings_svc.set_default_model("orchestrator", model.address)

    # Simulate get_provider_by_id failing with provider_not_found
    async def fake_get_provider_by_id(provider_id: str) -> NoReturn:
        raise DomainError("provider_not_found", f"No provider {provider_id}.", status=404)

    monkeypatch.setattr(providers_svc, "get_provider_by_id", fake_get_provider_by_id)

    # get reflects model_missing
    view = await settings_svc.get("orchestrator")
    expected_msg = (
        f"The model {model.address} no longer exists. Choose another model for Orchestrator."
    )
    assert view.model_problem == expected_msg

    # resolve also raises model_missing
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "model_missing"
    assert err.value.message == expected_msg


async def test_check_model_reraises_other_domain_errors(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    databases: Databases,
    fake_openai: RunningFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = fake_openai.base_url
    await databases.project("prj_1")
    await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)
    model = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(model.address)
    await settings_svc.set_default_model("orchestrator", model.address)

    # Simulate get_provider_by_id failing with internal_error (status=500)
    async def fake_get_provider_by_id(provider_id: str) -> NoReturn:
        raise DomainError("internal_error", "Something went wrong", status=500)

    monkeypatch.setattr(providers_svc, "get_provider_by_id", fake_get_provider_by_id)

    # get() re-raises DomainError directly
    with pytest.raises(DomainError) as err_get:
        await settings_svc.get("orchestrator")
    assert err_get.value.status == 500
    assert err_get.value.code == "internal_error"
    assert err_get.value.message == "Something went wrong"

    # resolve() also re-raises DomainError directly
    with pytest.raises(DomainError) as err_res:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err_res.value.status == 500
    assert err_res.value.code == "internal_error"
    assert err_res.value.message == "Something went wrong"


def test_internal_models_are_private() -> None:
    import mlops_agents.agents.settings as settings_mod

    assert not hasattr(settings_mod, "ModelProblem")
    assert not hasattr(settings_mod, "ResolvedTarget")
    assert hasattr(settings_mod, "_ModelProblem")
    assert hasattr(settings_mod, "_ResolvedTarget")


async def test_check_model_raises_runtime_error_if_context_window_missing(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    databases: Databases,
    fake_openai: RunningFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = fake_openai.base_url
    await databases.project("prj_1")
    await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)
    model = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(model.address)
    await settings_svc.set_default_model("orchestrator", model.address)

    # Corrupt model entry so context_window is None, and bypass problem computation
    from mlops_agents.providers.models import ModelEntry

    monkeypatch.setattr(ModelEntry, "problem", property(lambda self: None))

    real_get_model = providers_svc.get_model

    async def fake_get_model(address: str) -> ModelEntry | None:
        entry = await real_get_model(address)
        if entry is not None:
            return entry.model_copy(update={"context_window": None})
        return None

    monkeypatch.setattr(providers_svc, "get_model", fake_get_model)

    with pytest.raises(RuntimeError, match="passed usability check but context_window is None"):
        await settings_svc.resolve("prj_1", "orchestrator")


async def test_setters_accept_model_when_provider_needs_attention(
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    databases: Databases,
    fake_openai: RunningFake,
) -> None:
    base_url = fake_openai.base_url
    await databases.project("prj_1")

    provider = await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=base_url)
    )
    await providers_svc.add_key("groq", SECRET_KEY_1)
    model = await providers_svc.add_model(
        ModelCreate(provider="groq", wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(model.address)

    # Put provider in needs_attention
    await providers_svc.mark_needs_attention(provider.id, "Account billing suspended")
    expected_msg = "groq needs attention: Account billing suspended. Fix it in AI providers."

    # set_default_model succeeds even though provider needs attention
    view = await settings_svc.set_default_model("orchestrator", model.address)
    assert view.default_model == model.address
    assert view.effective_model == model.address
    assert view.model_problem == expected_msg

    # get also reflects provider_needs_attention
    get_view = await settings_svc.get("orchestrator", "prj_1")
    assert get_view.model_problem == expected_msg

    # resolve raises provider_needs_attention
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "provider_needs_attention"
    assert err.value.message == expected_msg

    # Clear default
    await settings_svc.set_default_model("orchestrator", None)

    # set_project_model also succeeds when provider needs attention
    p_view = await settings_svc.set_project_model("prj_1", "orchestrator", model.address)
    assert p_view.project_model == model.address
    assert p_view.effective_model == model.address
    assert p_view.model_problem == expected_msg

    # resolve still raises provider_needs_attention
    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "provider_needs_attention"
    assert err.value.message == expected_msg


async def test_embedder_model_rejected_for_agents(
    databases: Databases,
    settings_svc: AgentSettingsService,
    providers_svc: ProvidersService,
    fake_openai: RunningFake,
) -> None:
    # 1. Add self-hosted provider and verified embedder model
    await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    model = await providers_svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-embed", model_type="embedder")
    )
    await providers_svc.verify_model(model.address)

    # 2. set_default_model refuses embedder with 409 model_not_usable
    with pytest.raises(DomainError) as err:
        await settings_svc.set_default_model("orchestrator", model.address)
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    assert "is an embedder model, not a chat model. Choose a chat model." in err.value.message

    # 3. set_project_model refuses embedder with 409 model_not_usable
    with pytest.raises(DomainError) as err:
        await settings_svc.set_project_model("prj_1", "orchestrator", model.address)
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    assert "is an embedder model, not a chat model. Choose a chat model." in err.value.message

    # 4. If an agent's default in DB was pointing to an embedder, get and resolve report the problem
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            sqlite_insert(agent_model_defaults)
            .values(agent="orchestrator", model_address=model.address)
            .on_conflict_do_update(
                index_elements=[agent_model_defaults.c.agent],
                set_={"model_address": model.address},
            )
        )

    view = await settings_svc.get("orchestrator")
    assert view.model_problem == (
        f"{model.address} is an embedder model, not a chat model. Choose a chat model."
    )

    with pytest.raises(DomainError) as err:
        await settings_svc.resolve("prj_1", "orchestrator")
    assert err.value.status == 409
    assert err.value.code == "model_not_usable"
    assert (
        f"{model.address} is an embedder model, not a chat model. Choose a chat model."
        in err.value.message
    )
