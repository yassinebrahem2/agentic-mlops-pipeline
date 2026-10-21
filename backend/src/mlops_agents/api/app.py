"""The FastAPI application: services, error format, access guard and discovered routers."""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import secrets
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from types import ModuleType

from fastapi import APIRouter, FastAPI

from mlops_agents.api import routers
from mlops_agents.api.errors import InternalErrors, register_error_handlers
from mlops_agents.api.hosts import HostCheck, HostRules
from mlops_agents.api.routers.auth import LoginLimiter, PasswordHash
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
            if settings.password is not None:
                app.state.password_hash = await PasswordHash.of(settings.password)
            yield

    app = FastAPI(
        title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    # Each start has a new token unless the settings give one; the CLI prints it in the URL.
    app.state.access_token = settings.access_token or secrets.token_urlsafe(32)
    app.state.host_rules = rules = HostRules(settings)
    app.state.login_limiter = LoginLimiter()
    register_error_handlers(app)
    include_routers(app, routers)
    # The last middleware added is the outermost. The catch-all 500 wraps everything of ours,
    # so an error in a check is the JSON 500 too; a bad Host or Origin is refused before the
    # session is looked at.
    app.add_middleware(SessionGuard)
    app.add_middleware(HostCheck, rules=rules)
    app.add_middleware(InternalErrors)
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
