"""The FastAPI application: services, error format, access guard and discovered routers."""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from types import ModuleType

from fastapi import APIRouter, FastAPI

from mlops_agents.api import routers
from mlops_agents.api.errors import InternalErrors, register_error_handlers
from mlops_agents.api.security import SessionGuard, SessionSigner, load_secret
from mlops_agents.api.services import build_services
from mlops_agents.config import APP_NAME, Settings
from mlops_agents.credentials import KeyStore
from mlops_agents.integrations.github.auth import GhLike


def create_app(
    settings: Settings, *, keystore: KeyStore | None = None, gh: GhLike | None = None
) -> FastAPI:
    """Builds the app without I/O; the lifespan touches the data folder, key store and network."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with AsyncExitStack() as stack:
            services = await build_services(stack, settings, keystore=keystore, gh=gh)
            app.state.services = services
            app.state.signer = SessionSigner(await asyncio.to_thread(load_secret, services.paths))
            yield

    app = FastAPI(
        title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    register_error_handlers(app)
    include_routers(app, routers)
    # The last middleware added is the outermost: the guard runs first, and the catch-all
    # 500 sits innermost around the routers.
    app.add_middleware(InternalErrors)
    app.add_middleware(SessionGuard)
    return app


def include_routers(app: FastAPI, package: ModuleType) -> None:
    """Includes the `router` of every module in `package` under /api, in module-name order so
    the OpenAPI output is stable."""
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        name = f"{package.__name__}.{info.name}"
        router: object = getattr(importlib.import_module(name), "router", None)
        if not isinstance(router, APIRouter):
            raise RuntimeError(f"{name} must define `router: APIRouter` to be served under /api.")
        app.include_router(router, prefix="/api")
