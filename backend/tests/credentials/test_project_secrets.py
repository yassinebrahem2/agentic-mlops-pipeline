from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.config import RunMode, load_settings
from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    project_prefix,
    proposal_prefix,
    proposal_slot_ref,
    tracking_ref,
)
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import secret_refs
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.integrations.test_github_auth import FakeGh

VALUE = "s3cr3t-value-never-shown"


class NoReadKeyStore(MemoryKeyStore):
    def get(self, ref: str) -> str | None:
        raise KeyStoreError("reading the key store is not allowed in this test")


class FailingDeleteKeyStore(MemoryKeyStore):
    def delete(self, ref: str) -> None:
        raise KeyStoreError("the key store is locked")


class FailingSetKeyStore(MemoryKeyStore):
    def set(self, ref: str, value: str) -> None:
        raise KeyStoreError("the key store is locked")


async def _rows(databases: Databases) -> list[tuple[str, str]]:
    engine = await databases.app()
    async with engine.connect() as conn:
        result = await conn.execute(
            select(secret_refs.c.ref, secret_refs.c.project).order_by(secret_refs.c.ref)
        )
        return [(str(r[0]), str(r[1])) for r in result]


def test_ref_builders() -> None:
    assert project_prefix("prj_1") == "project/prj_1/"
    assert proposal_prefix("prj_1", "prp_2") == "project/prj_1/proposal/prp_2/"
    assert proposal_slot_ref("prj_1", "prp_2", 0) == "project/prj_1/proposal/prp_2/0"
    assert tracking_ref("prj_1", "trk_3") == "project/prj_1/tracking/trk_3"


async def test_put_then_move_leaves_one_ref(databases: Databases, keystore: MemoryKeyStore) -> None:
    secrets = ProjectSecrets(keystore, databases)
    slot = proposal_slot_ref("prj_1", "prp_1", 0)
    target = tracking_ref("prj_1", "trk_1")
    await secrets.put(slot, VALUE)
    await secrets.move(slot, target)
    assert keystore.data == {target: VALUE}
    assert await _rows(databases) == [(target, "prj_1")]
    assert not await secrets.is_set(slot)
    assert await secrets.is_set(target)


async def test_put_twice_overwrites_and_records_once(
    databases: Databases, keystore: MemoryKeyStore
) -> None:
    secrets = ProjectSecrets(keystore, databases)
    ref = tracking_ref("prj_1", "trk_1")
    await secrets.put(ref, "first")
    await secrets.put(ref, VALUE)
    assert keystore.data == {ref: VALUE}
    assert await _rows(databases) == [(ref, "prj_1")]


async def test_is_set_never_reads_the_keystore(databases: Databases) -> None:
    secrets = ProjectSecrets(NoReadKeyStore(), databases)
    ref = tracking_ref("prj_1", "trk_1")
    assert not await secrets.is_set(ref)
    await secrets.put(ref, VALUE)
    assert await secrets.is_set(ref)
    await secrets.delete(ref)
    assert not await secrets.is_set(ref)


async def test_delete_prefix_removes_only_refs_under_it(
    databases: Databases, keystore: MemoryKeyStore
) -> None:
    secrets = ProjectSecrets(keystore, databases)
    inside = [
        proposal_slot_ref("prj_1", "prp_1", 0),
        proposal_slot_ref("prj_1", "prp_1", 1),
        tracking_ref("prj_1", "trk_1"),
    ]
    # prjX1 matches project/prj_1/ when "_" is read as a LIKE wildcard; prj_10 shares its start.
    outside = [tracking_ref("prjX1", "trk_1"), tracking_ref("prj_10", "trk_1")]
    for ref in inside + outside:
        await secrets.put(ref, VALUE)

    await secrets.delete_prefix(proposal_prefix("prj_1", "prp_1"))
    assert set(keystore.data) == {tracking_ref("prj_1", "trk_1"), *outside}

    await secrets.delete_prefix(project_prefix("prj_1"))
    assert set(keystore.data) == set(outside)
    assert [ref for ref, _ in await _rows(databases)] == sorted(outside)


async def test_keystore_failure_on_delete_keeps_the_row(databases: Databases) -> None:
    keystore = FailingDeleteKeyStore()
    secrets = ProjectSecrets(keystore, databases)
    ref = tracking_ref("prj_1", "trk_1")
    await secrets.put(ref, VALUE)
    with pytest.raises(KeyStoreError) as err:
        await secrets.delete(ref)
    assert VALUE not in str(err.value)
    assert await secrets.is_set(ref)
    with pytest.raises(KeyStoreError):
        await secrets.delete_prefix(project_prefix("prj_1"))
    assert await _rows(databases) == [(ref, "prj_1")]


async def test_keystore_failure_on_put_records_nothing(databases: Databases) -> None:
    secrets = ProjectSecrets(FailingSetKeyStore(), databases)
    ref = tracking_ref("prj_1", "trk_1")
    with pytest.raises(KeyStoreError) as err:
        await secrets.put(ref, VALUE)
    assert VALUE not in str(err.value)
    assert not await secrets.is_set(ref)


@pytest.mark.parametrize("ref", ["provider/prv_1/abc", "project/", "project/prj_1", "x"])
async def test_put_refuses_a_ref_outside_a_project(
    databases: Databases, keystore: MemoryKeyStore, ref: str
) -> None:
    secrets = ProjectSecrets(keystore, databases)
    with pytest.raises(ValueError) as err:
        await secrets.put(ref, VALUE)
    assert VALUE not in str(err.value)
    assert keystore.data == {}


async def test_move_of_an_unset_ref_fails_without_the_value(
    databases: Databases, keystore: MemoryKeyStore
) -> None:
    secrets = ProjectSecrets(keystore, databases)
    target = tracking_ref("prj_1", "trk_1")
    await secrets.put(target, VALUE)
    with pytest.raises(LookupError) as err:
        await secrets.move(proposal_slot_ref("prj_1", "prp_1", 0), target)
    assert VALUE not in str(err.value)
    assert keystore.data == {target: VALUE}


async def test_removing_a_project_deletes_only_its_secrets(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    secrets = ProjectSecrets(keystore, databases)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    workspace = WorkspaceService(
        settings, paths, databases, GitRunner(paths.bin, github.token), github, http, secrets
    )
    try:
        removed = await workspace.add_local(str(make_repo(tmp_path / "a", {"a": "1"}, git_env)))
        kept = await workspace.add_local(str(make_repo(tmp_path / "b", {"b": "1"}, git_env)))
        for project_id in (removed.id, kept.id):
            await secrets.put(tracking_ref(project_id, "trk_1"), VALUE)
            await secrets.put(proposal_slot_ref(project_id, "prp_1", 0), VALUE)

        await workspace.remove(removed.id)

        assert all(ref.startswith(project_prefix(kept.id)) for ref in keystore.data)
        assert len(keystore.data) == 2
        assert {project for _, project in await _rows(databases)} == {kept.id}
    finally:
        await http.aclose()
