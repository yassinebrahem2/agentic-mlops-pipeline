import importlib
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.api.app import create_app, include_routers
from mlops_agents.api.services import Services
from mlops_agents.config import RunMode, load_settings
from tests.api.conftest import NoGh
from tests.conftest import MemoryKeyStore


async def test_lifespan_starts_and_stops_on_a_temporary_data_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[httpx2.AsyncClient] = []

    class RecordedHttpx2(httpx2.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.append(self)

    monkeypatch.setattr(httpx2, "AsyncClient", RecordedHttpx2)
    data = tmp_path / "data"
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(data)})
    app = create_app(settings, keystore=MemoryKeyStore(), gh=NoGh())

    async with app.router.lifespan_context(app):
        services: Services = app.state.services
        assert (data / "app.db").exists()
        assert services.paths.root == data
        assert services.catalog.all()
        assert len(opened) == 1
        # The services keep the client privately; vars() reads it without a private-access
        # suppression, and identity is what proves they share one client.
        shared = [
            vars(service)["_http"]
            for service in (services.providers, services.github, services.workspace)
        ]
        assert all(client is opened[0] for client in shared)
        assert not any(client.is_closed for client in opened)

    assert all(client.is_closed for client in opened)


def _package(root: Path, name: str, modules: dict[str, str]) -> None:
    folder = root / name
    folder.mkdir()
    (folder / "__init__.py").write_text("", encoding="utf-8")
    for module, source in modules.items():
        (folder / f"{module}.py").write_text(source, encoding="utf-8")


ROUTER_SOURCE = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n"
    "@router.get('/{name}')\n"
    "async def hello() -> dict[str, str]:\n"
    "    return {'from': '{name}'}\n"
)


async def test_router_modules_are_included_under_api_in_name_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    _package(
        tmp_path,
        "discovered_routers",
        {
            "zeta": ROUTER_SOURCE.replace("{name}", "zeta"),
            "alpha": ROUTER_SOURCE.replace("{name}", "alpha"),
        },
    )

    app = FastAPI()

    include_routers(app, importlib.import_module("discovered_routers"))

    assert list(app.openapi()["paths"]) == ["/api/alpha", "/api/zeta"]
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.get("/api/zeta")
    assert response.json() == {"from": "zeta"}


def test_a_router_module_without_router_fails_with_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    _package(tmp_path, "broken_routers", {"lonely": "VALUE = 1\n"})

    with pytest.raises(RuntimeError, match=r"broken_routers\.lonely"):
        include_routers(FastAPI(), importlib.import_module("broken_routers"))


def test_create_app_touches_no_file(tmp_path: Path) -> None:
    data = tmp_path / "not-created"
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(data)})

    create_app(settings)

    assert not data.exists()
    assert list(tmp_path.iterdir()) == []


async def test_server_client_is_signed_in_to_a_server_mode_app_over_https(
    server_client: httpx2.AsyncClient,
) -> None:
    response = await server_client.post("/api/nope")

    assert str(response.request.url) == "https://app.example.com/api/nope"
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
