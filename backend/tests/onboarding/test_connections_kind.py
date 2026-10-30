"""The `connections` proposal kind: each approved entry becomes a connection, idempotently."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.onboarding.connections_kind import ConnectionsKind
from mlops_agents.onboarding.kinds_common import PROJECT_KEY
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import CONNECTIONS_KIND
from mlops_agents.proposals.payloads import ConnectionsPayload
from mlops_agents.storage.databases import Databases

PROJECT = "prj_connections"
PROPOSAL = "prp_connections"


async def _no_secrets() -> list[str]:
    return []


@pytest.fixture
async def service(databases: Databases, paths: DataPaths) -> AsyncIterator[ConnectionsService]:
    await databases.project(PROJECT)
    yield ConnectionsService(
        databases, SqliteEventLog(databases), KindRegistry([LocalFolderKind(paths.root)])
    )


@pytest.fixture
def kind(service: ConnectionsService) -> ConnectionsKind:
    return ConnectionsKind(service, _no_secrets)


def _folder(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "bookings.csv").write_text("id,day\n1,2026-10-01\n", encoding="utf-8")
    return folder


def _entry(name: str, path: Path) -> dict[str, Any]:
    return {
        "kind": "local_folder",
        "name": name,
        "settings": {"path": str(path)},
        "why": f"The {name} exports",
    }


def _payload(*entries: dict[str, Any]) -> ConnectionsPayload:
    return ConnectionsPayload.model_validate({"connections": list(entries)})


def test_the_kind_is_named_by_the_contract_constant(kind: ConnectionsKind) -> None:
    assert kind.name == CONNECTIONS_KIND
    assert kind.payload_model is ConnectionsPayload


def test_key_warnings_and_credentials_are_constant(kind: ConnectionsKind, tmp_path: Path) -> None:
    payload = _payload(_entry("hotel", tmp_path / "hotel"))
    assert kind.key(payload) == PROJECT_KEY
    assert kind.warnings(payload) == []
    assert kind.credentials_needed(payload) == []


def test_preview_names_each_entry_its_kind_path_and_why(
    kind: ConnectionsKind, tmp_path: Path
) -> None:
    hotel, crm = tmp_path / "hotel", tmp_path / "crm"
    preview = kind.preview(_payload(_entry("hotel", hotel), _entry("crm", crm)))
    assert preview.kind == "text"
    assert preview.title == "Connections"
    for name, path in (("hotel", hotel), ("crm", crm)):
        assert name in preview.body
        assert str(path) in preview.body
        assert f"The {name} exports" in preview.body
    assert "local_folder" in preview.body


async def test_approving_two_entries_creates_both(
    kind: ConnectionsKind, service: ConnectionsService, tmp_path: Path
) -> None:
    payload = _payload(
        _entry("hotel", _folder(tmp_path, "hotel")), _entry("crm", _folder(tmp_path, "crm"))
    )

    result = await kind.apply(PROJECT, PROPOSAL, payload)

    assert result.applied == ["connection:hotel", "connection:crm"]
    assert result.failed == {}
    assert sorted(c.name for c in await service.list(PROJECT)) == ["crm", "hotel"]


async def test_a_folder_inside_the_data_folder_fails_and_the_other_is_created(
    kind: ConnectionsKind, service: ConnectionsService, paths: DataPaths, tmp_path: Path
) -> None:
    inside = _folder(paths.root, "inside")
    payload = _payload(_entry("inside", inside), _entry("hotel", _folder(tmp_path, "hotel")))

    result = await kind.apply(PROJECT, PROPOSAL, payload)

    assert result.applied == ["connection:hotel"]
    assert list(result.failed) == ["connection:inside"]
    assert "data folder" in result.failed["connection:inside"]
    assert [c.name for c in await service.list(PROJECT)] == ["hotel"]


async def test_a_missing_path_fails_with_its_reason(kind: ConnectionsKind, tmp_path: Path) -> None:
    result = await kind.apply(PROJECT, PROPOSAL, _payload(_entry("gone", tmp_path / "gone")))

    assert result.applied == []
    assert result.failed["connection:gone"]


async def test_the_same_call_twice_creates_one_connection(
    kind: ConnectionsKind, service: ConnectionsService, tmp_path: Path
) -> None:
    payload = _payload(_entry("hotel", _folder(tmp_path, "hotel")))

    first = await kind.apply(PROJECT, PROPOSAL, payload)
    second = await kind.apply(PROJECT, PROPOSAL, payload)

    assert first.applied == second.applied == ["connection:hotel"]
    assert second.failed == {}
    assert [c.name for c in await service.list(PROJECT)] == ["hotel"]


async def test_same_name_with_other_settings_fails_and_leaves_the_existing_connection(
    kind: ConnectionsKind, service: ConnectionsService, tmp_path: Path
) -> None:
    first = _folder(tmp_path, "first")
    await kind.apply(PROJECT, PROPOSAL, _payload(_entry("hotel", first)))

    result = await kind.apply(
        PROJECT, "prp_other", _payload(_entry("hotel", _folder(tmp_path, "second")))
    )

    assert result.applied == []
    assert result.failed == {
        "connection:hotel": "a connection named hotel already exists with other settings"
    }
    [existing] = await service.list(PROJECT)
    assert existing.settings == {"path": str(first)}
