"""Stored settings that no longer parse: one bad row never breaks the others or leaks its values."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import update

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
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
async def test_mounts_skip_a_bad_row_without_logging_its_values(
    service_env: ServiceEnv,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    stored: str,
) -> None:
    service = service_env[0]
    text = _drifted(tmp_path) if stored == "drifted" else "{not json " + SECRET
    await _good_and_bad(service_env, tmp_path, "prj_mounts", text)

    with caplog.at_level(logging.DEBUG, logger="mlops_agents.connections.service"):
        mounts = await service.mounts("prj_mounts")

    assert [m.container_path for m in mounts] == ["/data/good"]
    assert "Skipping mount for connection bad" in caplog.text
    assert SECRET not in caplog.text


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
