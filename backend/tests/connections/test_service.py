"""Tests for ConnectionsService."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, Findings
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.errors import DomainError
from mlops_agents.storage.tables import connections
from tests.connections.conftest import ServiceEnv


async def test_service_lifecycle(service_env: ServiceEnv, tmp_path: Path) -> None:
    service, databases, events, _paths = service_env
    project_id = "prj_test1"
    # Ensure project db exists
    await databases.project(project_id)

    data_dir = tmp_path / "data1"
    data_dir.mkdir()
    (data_dir / "users.csv").write_text("id,name\n1,alice", encoding="utf-8")

    # 1. Inspect writes no DB row and records no event
    inspect_res = await service.inspect(project_id, LOCAL_FOLDER_KIND, {"path": str(data_dir)})
    assert inspect_res.truncated is False
    conns_after_inspect = await service.list(project_id)
    assert len(conns_after_inspect) == 0
    all_events = await events.history(
        project_id, types=["connection.created", "connection.updated", "connection.deleted"]
    )
    assert len(all_events) == 0

    # 2. Create validation errors
    # Invalid name
    with pytest.raises(DomainError) as err_name:
        await service.create(
            project_id, LOCAL_FOLDER_KIND, "Invalid_Name!", {"path": str(data_dir)}
        )
    assert err_name.value.code == "invalid_connection_name"

    # Unknown kind
    with pytest.raises(DomainError) as err_kind:
        await service.create(project_id, "unknown_kind", "valid-name", {"path": str(data_dir)})
    assert err_kind.value.code == "unknown_connection_kind"

    # Invalid settings (relative path)
    with pytest.raises(DomainError) as err_settings:
        await service.create(project_id, LOCAL_FOLDER_KIND, "valid-name", {"path": "relative/path"})
    assert err_settings.value.code == "connection_invalid"

    # 3. Create success
    view = await service.create(project_id, LOCAL_FOLDER_KIND, "my-data", {"path": str(data_dir)})
    assert view.name == "my-data"
    assert view.kind == LOCAL_FOLDER_KIND
    assert view.status == "ok"
    assert view.status_detail is None
    assert view.data_version is not None

    # Check event
    evs = await events.history(project_id, types=["connection.created"])
    assert len(evs) == 1
    assert evs[0].type == "connection.created"
    assert evs[0].subject_id == view.id
    assert evs[0].payload == {"name": "my-data", "kind": LOCAL_FOLDER_KIND}

    # 4. Duplicate name -> 409
    with pytest.raises(DomainError) as err_dup:
        await service.create(project_id, LOCAL_FOLDER_KIND, "my-data", {"path": str(data_dir)})
    assert err_dup.value.code == "connection_name_taken"

    # 5. List
    listing = await service.list(project_id)
    assert len(listing) == 1
    assert listing[0].id == view.id
    assert listing[0].status == "ok"

    # 6. Tables
    tables = await service.tables(project_id, view.id)
    assert len(tables) == 1
    assert tables[0].name == "users"

    # 7. Data version and duckdb source
    v = await service.data_version(project_id, view.id, "users")
    assert v == listing[0].data_version
    source = await service.duckdb_source(project_id, view.id, "users")
    assert "users.csv" in source.sql_from

    # Table lookup fails on unknown or traversal
    with pytest.raises(DomainError) as err_tab:
        await service.data_version(project_id, view.id, "../etc/passwd")
    assert err_tab.value.code == "table_not_found"

    # 8. Update
    data_dir2 = tmp_path / "data2"
    data_dir2.mkdir()
    (data_dir2 / "items.parquet").write_bytes(b"PAR1")

    updated = await service.update(project_id, view.id, {"path": str(data_dir2)})
    assert updated.id == view.id
    assert updated.name == view.name
    assert updated.settings == {"path": str(data_dir2)}

    evs2 = await events.history(project_id, types=["connection.updated"])
    assert len(evs2) == 1
    assert evs2[0].type == "connection.updated"
    assert evs2[0].subject_id == view.id

    # 9. Mounts
    mounts = await service.mounts(project_id)
    assert len(mounts) == 1
    assert mounts[0].container_path == "/data/my-data"
    assert mounts[0].host_path == data_dir2.resolve()

    # 10. Delete
    await service.delete(project_id, view.id)
    evs3 = await events.history(project_id, types=["connection.deleted"])
    assert len(evs3) == 1
    assert evs3[0].type == "connection.deleted"
    assert evs3[0].subject_id == view.id

    all_con_events = await events.history(
        project_id, types=["connection.created", "connection.updated", "connection.deleted"]
    )
    assert len(all_con_events) == 3

    # Post-delete 404
    with pytest.raises(DomainError) as err_del:
        await service.tables(project_id, view.id)
    assert err_del.value.code == "connection_not_found"


async def test_service_unreachable_handling(service_env: ServiceEnv, tmp_path: Path) -> None:
    service, databases, _, _ = service_env
    project_id = "prj_test2"
    await databases.project(project_id)

    # Create two connections
    d1 = tmp_path / "d1"
    d1.mkdir()
    (d1 / "a.csv").write_text("x\n1", encoding="utf-8")

    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "b.csv").write_text("y\n2", encoding="utf-8")

    c1 = await service.create(project_id, LOCAL_FOLDER_KIND, "c1", {"path": str(d1)})
    c2 = await service.create(project_id, LOCAL_FOLDER_KIND, "c2", {"path": str(d2)})

    # Delete d1 on disk
    shutil.rmtree(d1)

    listing = await service.list(project_id)
    assert len(listing) == 2
    item1 = next(x for x in listing if x.id == c1.id)
    item2 = next(x for x in listing if x.id == c2.id)

    assert item1.status == "unreachable"
    assert item1.status_detail == f"Folder does not exist: {d1}"
    assert item1.data_version is None
    assert item1.findings.unsupported_files == []

    assert item2.status == "ok"
    assert item2.status_detail is None

    # Mounts fails when a connection is unreachable
    with pytest.raises(DomainError) as exc_info:
        await service.mounts(project_id)
    assert exc_info.value.code == "connection_unreachable"
    assert "c1" in exc_info.value.message
    assert f"Folder does not exist: {d1}" in exc_info.value.message


async def test_service_list_data_version_updates_on_mtime_change(
    service_env: ServiceEnv, tmp_path: Path
) -> None:
    service, databases, _, _ = service_env
    project_id = "prj_test3"
    await databases.project(project_id)

    data_dir = tmp_path / "data_mtime"
    data_dir.mkdir()
    file_path = data_dir / "records.csv"
    file_path.write_text("id,val\n1,a", encoding="utf-8")

    c = await service.create(project_id, LOCAL_FOLDER_KIND, "conn", {"path": str(data_dir)})
    v1 = c.data_version
    assert v1 is not None

    listing1 = await service.list(project_id)
    assert listing1[0].data_version == v1

    # Update mtime on records.csv
    import asyncio
    import os

    await asyncio.sleep(0.01)
    st = file_path.stat()
    os.utime(file_path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))

    listing2 = await service.list(project_id)
    v2 = listing2[0].data_version
    assert v2 is not None
    assert v2 != v1


async def test_a_name_with_a_trailing_newline_is_refused(
    service_env: ServiceEnv, tmp_path: Path
) -> None:
    service, databases, _, _ = service_env
    await databases.project("prj_name")
    data_dir = tmp_path / "d"
    data_dir.mkdir()

    with pytest.raises(DomainError) as err:
        await service.create("prj_name", LOCAL_FOLDER_KIND, "abc\n", {"path": str(data_dir)})
    assert err.value.code == "invalid_connection_name"


async def test_settings_errors_never_echo_the_submitted_values(
    service_env: ServiceEnv, tmp_path: Path
) -> None:
    service, databases, _, _ = service_env
    await databases.project("prj_echo")
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    secret = "sk-secret-xyz-123"
    bad = {"path": str(data_dir), "token": secret}
    view = await service.create("prj_echo", LOCAL_FOLDER_KIND, "ok", {"path": str(data_dir)})

    for call in (
        service.inspect("prj_echo", LOCAL_FOLDER_KIND, bad),
        service.create("prj_echo", LOCAL_FOLDER_KIND, "new", bad),
        service.update("prj_echo", view.id, bad),
    ):
        with pytest.raises(DomainError) as err:
            await call
        assert err.value.code == "connection_invalid"
        assert err.value.status == 422
        assert err.value.detail == {
            "errors": [{"field": "token", "message": "Extra inputs are not permitted"}]
        }
        assert secret not in err.value.message
        assert secret not in repr(err.value.detail)


async def test_list_stores_and_returns_the_kind_error_scrubbed(
    service_env: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, databases, _, _ = service_env
    engine = await databases.project("prj_scrub")
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    view = await service.create("prj_scrub", LOCAL_FOLDER_KIND, "c", {"path": str(data_dir)})

    def leaky_findings(self: LocalFolderKind, settings: BaseModel) -> Findings:
        raise OSError("fetch https://user:hunter2@host/x?token=abc failed")

    monkeypatch.setattr(LocalFolderKind, "findings", leaky_findings)

    (listed,) = await service.list("prj_scrub")

    assert listed.status == "unreachable"
    assert listed.status_detail == "fetch https://***@host/x?token=*** failed"
    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(connections.c.status_detail).where(connections.c.id == view.id)
            )
        ).scalar_one()
    assert stored == listed.status_detail


async def test_list_lets_an_unexpected_error_through(
    service_env: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, databases, _, _ = service_env
    await databases.project("prj_bug")
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    await service.create("prj_bug", LOCAL_FOLDER_KIND, "c", {"path": str(data_dir)})

    def broken_findings(self: LocalFolderKind, settings: BaseModel) -> Findings:
        raise RuntimeError("a bug")

    monkeypatch.setattr(LocalFolderKind, "findings", broken_findings)

    with pytest.raises(RuntimeError):
        await service.list("prj_bug")


async def test_a_failure_while_reading_the_folder_creates_no_connection(
    service_env: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, databases, events, _ = service_env
    await databases.project("prj_late")
    data_dir = tmp_path / "d"
    data_dir.mkdir()

    def vanished(self: LocalFolderKind, settings: BaseModel) -> Findings:
        raise DomainError("connection_invalid", "Folder does not exist: x")

    monkeypatch.setattr(LocalFolderKind, "findings", vanished)

    with pytest.raises(DomainError) as err:
        await service.create("prj_late", LOCAL_FOLDER_KIND, "c", {"path": str(data_dir)})
    assert err.value.status == 400
    monkeypatch.undo()
    assert await service.list("prj_late") == []
    assert await events.history("prj_late", types=["connection.created"]) == []
