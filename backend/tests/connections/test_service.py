"""Tests for ConnectionsService."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases

ServiceEnv = tuple[ConnectionsService, Databases, SqliteEventLog, DataPaths]


@pytest.fixture
async def service_env(tmp_path: Path) -> AsyncIterator[ServiceEnv]:
    home = tmp_path / "home"
    home.mkdir()
    paths = DataPaths(home)
    paths.ensure()

    databases = Databases(paths)
    await databases.app()

    events = SqliteEventLog(databases)
    registry = KindRegistry([LocalFolderKind(paths.root)])
    service = ConnectionsService(databases, events, registry)

    yield service, databases, events, paths
    await databases.dispose()


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
    assert item1.status_detail is not None
    assert item1.data_version is None
    assert item1.findings.unsupported_files == []

    assert item2.status == "ok"
    assert item2.status_detail is None

    # Mounts leaves out unreachable connection
    mounts = await service.mounts(project_id)
    assert len(mounts) == 1
    assert mounts[0].container_path == "/data/c2"


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
