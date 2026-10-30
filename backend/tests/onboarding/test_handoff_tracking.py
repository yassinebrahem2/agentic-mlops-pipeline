"""The handoff's tracking parts reuse a source only when type, settings and auth all match
(Decision #538)."""

from __future__ import annotations

from typing import Any, Literal

from mlops_agents.credentials.project_secrets import proposal_slot_ref, tracking_ref
from mlops_agents.proposals.payloads import HandoffPayload
from tests.fakes.fake_mlflow import RunningFakeMlflow
from tests.onboarding.handoff_env import PROJECT, PROPOSAL, HandoffEnv, payload

SECRET = "trk-Vb52qLx8-secret"


def _one_source(url: str, auth: Literal["none", "credential"]) -> HandoffPayload:
    tracking: list[dict[str, Any]] = [{"type": "mlflow", "settings": {"url": url}, "auth": auth}]
    return payload(url, tracking=tracking)


async def _secret_refs(henv: HandoffEnv) -> list[str]:
    return sorted(henv.keystore.data)


async def test_a_source_asking_for_a_credential_is_created_beside_an_open_one(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    old = await henv.tracking.create_source(
        PROJECT, "mlflow", {"url": mlflow.base_url}, auth="none"
    )
    slot = proposal_slot_ref(PROJECT, PROPOSAL, 0)
    await henv.secrets.put(slot, SECRET)

    result = await henv.kind().apply(PROJECT, PROPOSAL, _one_source(mlflow.base_url, "credential"))

    assert "tracking:0" in result.applied
    assert "tracking:0" not in result.failed
    sources = {s.id: s for s in await henv.tracking.sources(PROJECT)}
    assert len(sources) == 2
    [new] = [s for s in sources.values() if s.id != old.id]
    assert (new.auth, new.credential_set) == ("credential", True)
    assert sources[old.id].auth == "none"
    assert henv.keystore.get(tracking_ref(PROJECT, new.id)) == SECRET
    assert henv.keystore.get(tracking_ref(PROJECT, old.id)) is None
    assert henv.keystore.get(slot) is None
    assert not await henv.secrets.is_set(slot)
    assert await _secret_refs(henv) == [tracking_ref(PROJECT, new.id)]


async def test_the_same_handoff_twice_reuses_the_new_source(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    await henv.tracking.create_source(PROJECT, "mlflow", {"url": mlflow.base_url}, auth="none")
    await henv.secrets.put(proposal_slot_ref(PROJECT, PROPOSAL, 0), SECRET)
    approved = _one_source(mlflow.base_url, "credential")
    kind = henv.kind()

    await kind.apply(PROJECT, PROPOSAL, approved)
    second = await kind.apply(PROJECT, PROPOSAL, approved)

    assert "tracking:0" in second.applied
    sources = await henv.tracking.sources(PROJECT)
    assert sorted(s.auth for s in sources) == ["credential", "none"]
    [new] = [s for s in sources if s.auth == "credential"]
    assert await _secret_refs(henv) == [tracking_ref(PROJECT, new.id)]
    assert henv.keystore.get(tracking_ref(PROJECT, new.id)) == SECRET


async def test_a_source_of_the_same_type_settings_and_auth_is_reused(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    existing = await henv.tracking.create_source(
        PROJECT, "mlflow", {"url": mlflow.base_url}, auth="credential"
    )
    await henv.secrets.put(proposal_slot_ref(PROJECT, PROPOSAL, 0), SECRET)

    result = await henv.kind().apply(PROJECT, PROPOSAL, _one_source(mlflow.base_url, "credential"))

    assert "tracking:0" in result.applied
    [source] = await henv.tracking.sources(PROJECT)
    assert source.id == existing.id
    assert henv.keystore.get(tracking_ref(PROJECT, existing.id)) == SECRET
