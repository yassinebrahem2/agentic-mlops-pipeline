"""Stored settings that no longer parse: one bad row never breaks the others or leaks its values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import text, update

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, LocalFolderSettings, Mount
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.errors import DomainError
from mlops_agents.storage.tables import connections
from tests.connections.conftest import ServiceEnv

SECRET = "sk-secret-stored-456"


async def _good_and_bad(env: ServiceEnv, tmp_path: Path, project_id: str, stored: str) -> str:
    """Creates connections `good` and `bad`, then overwrites bad's stored settings."""
    service, databases, _, _ = env
    engine = await databases.project(project_id)
    for name in ("good", "bad"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "t.csv").write_text("x\n1", encoding="utf-8")
        view = await service.create(project_id, LOCAL_FOLDER_KIND, name, {"path": str(folder)})
    async with engine.begin() as conn:
        await conn.execute(
            update(connections).where(connections.c.id == view.id).values(settings_json=stored)
        )
    return view.id


def _drifted(tmp_path: Path) -> str:
    return json.dumps({"path": str(tmp_path / "bad"), "token": SECRET})


async def test_list_shows_a_row_whose_settings_no_longer_parse_as_unreachable(
    service_env: ServiceEnv, tmp_path: Path
) -> None:
    service = service_env[0]
    await _good_and_bad(service_env, tmp_path, "prj_drift", _drifted(tmp_path))

    views = {v.name: v for v in await service.list("prj_drift")}

    assert views["good"].status == "ok"
    assert views["bad"].status == "unreachable"
    assert views["bad"].status_detail == (
        "The settings are invalid (token: Extra inputs are not permitted)."
    )
    assert views["bad"].data_version is None
    assert views["bad"].settings == {}
    assert SECRET not in views["bad"].model_dump_json()
    assert views["good"].settings == {"path": str(tmp_path / "good")}


async def test_list_keeps_the_parsed_settings_of_a_row_whose_folder_is_gone(
    service_env: ServiceEnv, tmp_path: Path
) -> None:
    service = service_env[0]
    await _good_and_bad(
        service_env, tmp_path, "prj_gone", json.dumps({"path": str(tmp_path / "missing")})
    )

    views = {v.name: v for v in await service.list("prj_gone")}

    assert views["bad"].status == "unreachable"
    assert views["bad"].settings == {"path": str(tmp_path / "missing")}


@pytest.mark.parametrize("stored", ["[]", '"x"', "1"])
async def test_list_shows_a_row_whose_settings_are_not_an_object_as_unreachable(
    service_env: ServiceEnv, tmp_path: Path, stored: str
) -> None:
    service = service_env[0]
    await _good_and_bad(service_env, tmp_path, "prj_obj", stored)

    views = {v.name: v for v in await service.list("prj_obj")}

    assert views["good"].status == "ok"
    assert views["bad"].status == "unreachable"
    assert views["bad"].status_detail == "The stored settings are not a JSON object."
    assert views["bad"].settings == {}


async def test_list_shows_a_row_whose_settings_are_not_text_as_unreachable(
    service_env: ServiceEnv, tmp_path: Path
) -> None:
    service, databases, _, _ = service_env
    con_id = await _good_and_bad(service_env, tmp_path, "prj_blob", "{}")
    engine = await databases.project("prj_blob")
    async with engine.begin() as conn:
        # A hand-edited row: SQLite keeps a blob as a blob even in a TEXT column.
        await conn.execute(
            text("UPDATE connections SET settings_json = X'FF' WHERE id = :id"), {"id": con_id}
        )

    views = {v.name: v for v in await service.list("prj_blob")}

    assert views["good"].status == "ok"
    assert views["bad"].status == "unreachable"
    assert views["bad"].status_detail == "The stored settings are not valid JSON."


async def test_list_shows_a_row_whose_settings_are_not_json_as_unreachable(
    service_env: ServiceEnv, tmp_path: Path
) -> None:
    service = service_env[0]
    await _good_and_bad(service_env, tmp_path, "prj_json", "{not json")

    views = {v.name: v for v in await service.list("prj_json")}

    assert views["good"].status == "ok"
    assert views["bad"].status == "unreachable"
    assert views["bad"].status_detail == "The stored settings are not valid JSON."
    assert views["bad"].settings == {}


@pytest.mark.parametrize("stored", ["drifted", "not json"])
async def test_mounts_on_a_bad_row_fails_with_scrubbed_error(
    service_env: ServiceEnv,
    tmp_path: Path,
    stored: str,
) -> None:
    service = service_env[0]
    text = _drifted(tmp_path) if stored == "drifted" else "{not json " + SECRET
    await _good_and_bad(service_env, tmp_path, "prj_mounts", text)

    with pytest.raises(DomainError) as err:
        await service.mounts("prj_mounts")
    assert err.value.code == "connection_unreachable"
    assert "bad" in err.value.message
    assert SECRET not in err.value.message
    assert SECRET not in repr(err.value.detail)


async def test_table_calls_on_a_bad_row_answer_a_safe_reason(
    service_env: ServiceEnv, tmp_path: Path
) -> None:
    service = service_env[0]
    con_id = await _good_and_bad(service_env, tmp_path, "prj_tables", _drifted(tmp_path))

    for call in (
        service.tables("prj_tables", con_id),
        service.data_version("prj_tables", con_id, "t"),
        service.duckdb_source("prj_tables", con_id, "t"),
    ):
        with pytest.raises(DomainError) as err:
            await call
        assert err.value.code == "connection_invalid"
        assert SECRET not in err.value.message
        assert SECRET not in repr(err.value.detail)


async def test_mounts_scrub_an_os_error_of_the_kind(
    service_env: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_env[0]
    await _good_and_bad(
        service_env, tmp_path, "prj_os", json.dumps({"path": str(tmp_path / "bad")})
    )

    def leaky_mount(self: LocalFolderKind, name: str, settings: BaseModel) -> Mount | None:
        raise OSError(f"mount https://user:{SECRET}@host/x?token={SECRET} failed")

    monkeypatch.setattr(LocalFolderKind, "sandbox_mount", leaky_mount)

    with pytest.raises(DomainError) as err:
        await service.mounts("prj_os")
    assert err.value.code == "connection_unreachable"
    assert SECRET not in err.value.message
    assert err.value.__cause__ is None
    assert "https://***@host/x?token=***" in err.value.message


async def test_mounts_hand_the_kind_only_parsed_settings(
    service_env: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_env[0]
    await _good_and_bad(
        service_env, tmp_path, "prj_val", json.dumps({"path": str(tmp_path / "bad")})
    )
    calls: list[tuple[str, BaseModel]] = []
    real_validate = LocalFolderKind.validate
    real_mount = LocalFolderKind.sandbox_mount

    def spy_validate(self: LocalFolderKind, settings: BaseModel, credential: str | None) -> None:
        calls.append(("validate", settings))
        real_validate(self, settings, credential)

    def spy_mount(self: LocalFolderKind, name: str, settings: BaseModel) -> Mount | None:
        calls.append(("mount", settings))
        return real_mount(self, name, settings)

    monkeypatch.setattr(LocalFolderKind, "validate", spy_validate)
    monkeypatch.setattr(LocalFolderKind, "sandbox_mount", spy_mount)

    mounts = await service.mounts("prj_val")

    assert [m.container_path for m in mounts] == ["/data/bad", "/data/good"]
    assert [step for step, _ in calls] == ["mount", "mount"]
    for _, settings in calls:
        assert isinstance(settings, LocalFolderSettings)
