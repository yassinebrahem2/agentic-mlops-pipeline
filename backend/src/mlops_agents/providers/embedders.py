"""Embedders service: project overrides and app defaults for embedding models (I14)."""

from __future__ import annotations

import httpx2
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.errors import DomainError
from mlops_agents.providers.embedder import Embedder, EmbedderStatus
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import Provider
from mlops_agents.providers.provider_embedder import ProviderEmbedder
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import app_settings, project_settings

EMBEDDER_KEY = "embedder.model"


class EmbeddersService:
    """Manages the app-level default embedder and project-level embedder overrides."""

    def __init__(
        self,
        databases: Databases,
        providers: ProvidersService,
        failover: KeyFailover,
        http: httpx2.AsyncClient,
    ) -> None:
        self._databases = databases
        self._providers = providers
        self._failover = failover
        self._http = http

    async def chosen_default(self) -> str | None:
        """The model address stored in the app settings, or None."""
        app_engine = await self._databases.app()
        async with app_engine.connect() as conn:
            res = await conn.scalar(
                select(app_settings.c.value).where(app_settings.c.key == EMBEDDER_KEY)
            )
            return str(res) if res is not None else None

    async def chosen_override(self, project_id: str) -> str | None:
        """The model address stored in the project settings, or None."""
        proj_engine = await self._databases.project(project_id)
        async with proj_engine.connect() as conn:
            res = await conn.scalar(
                select(project_settings.c.value).where(project_settings.c.key == EMBEDDER_KEY)
            )
            return str(res) if res is not None else None

    async def default_status(self) -> EmbedderStatus:
        """Status of the app-level default embedder alone."""
        address = await self.chosen_default()
        return await self._resolve_status(address)

    async def status(self, project_id: str) -> EmbedderStatus:
        """Effective embedder status for a project: override if set, else app default."""
        override = await self.chosen_override(project_id)
        if override is not None:
            return await self._resolve_status(override)
        default = await self.chosen_default()
        return await self._resolve_status(default)

    async def for_project(self, project_id: str) -> Embedder | None:
        """Returns the project's usable Embedder (override or default), or None."""
        st = await self.status(project_id)
        if (
            not st.configured
            or st.problem is not None
            or st.address is None
            or st.dimensions is None
        ):
            return None

        entry = await self._providers.require_model(st.address)
        provider = await self._providers.get_provider_by_id(entry.provider_id)
        return ProviderEmbedder(
            address=st.address,
            dimensions=st.dimensions,
            provider=provider,
            wire_model_id=entry.wire_model_id,
            failover=self._failover,
            providers=self._providers,
            http=self._http,
        )

    async def set_default(self, address: str | None) -> None:
        """Set or clear (None) the app-level default embedder."""
        if address is not None:
            await self._require_usable_embedder(address)
        app_engine = await self._databases.app()
        async with app_engine.begin() as conn:
            if address is None:
                await conn.execute(delete(app_settings).where(app_settings.c.key == EMBEDDER_KEY))
            else:
                await conn.execute(
                    sqlite_insert(app_settings)
                    .values(key=EMBEDDER_KEY, value=address)
                    .on_conflict_do_update(
                        index_elements=[app_settings.c.key],
                        set_={"value": address},
                    )
                )

    async def set_override(self, project_id: str, address: str | None) -> None:
        """Set or clear (None) the project-level embedder override."""
        if address is not None:
            await self._require_usable_embedder(address)
        proj_engine = await self._databases.project(project_id)
        async with proj_engine.begin() as conn:
            if address is None:
                await conn.execute(
                    delete(project_settings).where(project_settings.c.key == EMBEDDER_KEY)
                )
            else:
                await conn.execute(
                    sqlite_insert(project_settings)
                    .values(key=EMBEDDER_KEY, value=address)
                    .on_conflict_do_update(
                        index_elements=[project_settings.c.key],
                        set_={"value": address},
                    )
                )

    async def _resolve_status(self, address: str | None) -> EmbedderStatus:
        if address is None:
            return EmbedderStatus(
                configured=False,
                address=None,
                dimensions=None,
                problem=None,
            )

        entry = await self._providers.get_model(address)
        if entry is None:
            return EmbedderStatus(
                configured=True,
                address=address,
                dimensions=None,
                problem=f"The model {address} no longer exists. Choose another model.",
            )

        provider: Provider | None = None
        try:
            provider = await self._providers.get_provider_by_id(entry.provider_id)
        except DomainError as err:
            if err.code != "provider_not_found":
                raise

        if provider is None:
            return EmbedderStatus(
                configured=True,
                address=address,
                dimensions=None,
                problem=f"The provider for {address} no longer exists. Choose another model.",
            )

        problem = entry.problem_for("embedder")
        if problem is not None:
            return EmbedderStatus(
                configured=True,
                address=address,
                dimensions=None,
                problem=problem,
            )

        if provider.kind == "hosted" and not provider.keys:
            return EmbedderStatus(
                configured=True,
                address=address,
                dimensions=None,
                problem=f"{provider.name} has no key. Add one in AI providers.",
            )

        return EmbedderStatus(
            configured=True,
            address=address,
            dimensions=entry.dimensions,
            problem=None,
        )

    async def _require_usable_embedder(self, address: str) -> None:
        await self._providers.require_model(address)
        status = await self._resolve_status(address)
        if status.problem is not None:
            raise DomainError("model_not_usable", status.problem, status=409)
