from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.errors import DomainError
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.models import ModelEntry
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import agent_model_defaults, project_settings


class AgentLimitsUpdate(BaseModel):
    max_steps: int | None = Field(default=None, ge=1)  # None = no limit


class AgentSettingsView(BaseModel):
    agent: str
    display_name: str  # from the definition, for labels in the UI
    default_model: str | None  # app level
    project_model: str | None  # project override (None outside a project)
    effective_model: str | None
    model_problem: str | None  # plain text, e.g. the model no longer exists
    max_steps: int | None  # effective: project override, else the definition's


class AgentSettingsService:
    """Manages app-level default models and project-level model/limit overrides for agents.

    Callers pass a project id that exists (the API's ProjectDep and the runtime check it).
    The service never creates a project database for an unknown id on its own: it reads
    and writes only through databases.project(project_id) after the caller's check.
    """

    def __init__(
        self,
        databases: Databases,
        providers: ProvidersService,
        catalog: AgentCatalog,
    ) -> None:
        self._databases = databases
        self._providers = providers
        self._catalog = catalog

    async def get(self, agent: str, project_id: str | None = None) -> AgentSettingsView:
        definition = self._catalog.get(agent)
        default_model = await self._get_default_model(agent)
        if project_id is not None:
            project_model, max_steps = await self._get_project_settings(
                project_id, agent, definition
            )
        else:
            project_model = None
            max_steps = definition.limits.max_steps

        effective_model = project_model if project_model is not None else default_model
        model_problem = await self._compute_model_problem(definition, effective_model)

        return AgentSettingsView(
            agent=definition.name,
            display_name=definition.display_name,
            default_model=default_model,
            project_model=project_model,
            effective_model=effective_model,
            model_problem=model_problem,
            max_steps=max_steps,
        )

    async def list_settings(self, project_id: str | None = None) -> list[AgentSettingsView]:
        agents = sorted(self._catalog.all(), key=lambda d: d.name)
        return [await self.get(agent.name, project_id=project_id) for agent in agents]

    async def set_default_model(self, agent: str, address: str | None) -> AgentSettingsView:
        self._catalog.get(agent)
        app_engine = await self._databases.app()
        if address is not None:
            await self._check_model_usable(address)
            stmt = (
                sqlite_insert(agent_model_defaults)
                .values(agent=agent, model_address=address)
                .on_conflict_do_update(
                    index_elements=["agent"],
                    set_={"model_address": address},
                )
            )
            async with app_engine.begin() as conn:
                await conn.execute(stmt)
        else:
            async with app_engine.begin() as conn:
                await conn.execute(
                    delete(agent_model_defaults).where(agent_model_defaults.c.agent == agent)
                )
        return await self.get(agent, project_id=None)

    async def set_project_model(
        self, project_id: str, agent: str, address: str | None
    ) -> AgentSettingsView:
        self._catalog.get(agent)
        key = f"agent.{agent}.model"
        proj_engine = await self._databases.project(project_id)
        if address is not None:
            await self._check_model_usable(address)
            stmt = (
                sqlite_insert(project_settings)
                .values(key=key, value=address)
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": address},
                )
            )
            async with proj_engine.begin() as conn:
                await conn.execute(stmt)
        else:
            async with proj_engine.begin() as conn:
                await conn.execute(delete(project_settings).where(project_settings.c.key == key))
        return await self.get(agent, project_id=project_id)

    async def set_project_limits(
        self, project_id: str, agent: str, limits: AgentLimitsUpdate
    ) -> AgentSettingsView:
        self._catalog.get(agent)
        key = f"agent.{agent}.max_steps"
        value = json.dumps(limits.max_steps)
        proj_engine = await self._databases.project(project_id)
        stmt = (
            sqlite_insert(project_settings)
            .values(key=key, value=value)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value": value},
            )
        )
        async with proj_engine.begin() as conn:
            await conn.execute(stmt)
        return await self.get(agent, project_id=project_id)

    async def resolve(self, project_id: str, agent: str) -> tuple[ResolvedModel, int | None]:
        definition = self._catalog.get(agent)
        default_model = await self._get_default_model(agent)
        project_model, max_steps = await self._get_project_settings(project_id, agent, definition)
        effective_model = project_model if project_model is not None else default_model

        if effective_model is None:
            raise DomainError(
                "model_unassigned",
                f"Choose a model for {definition.display_name} in AI providers, "
                "or in this project's settings.",
                status=409,
            )

        entry = await self._providers.get_model(effective_model)
        if entry is None:
            raise DomainError(
                "model_missing",
                f"The model {effective_model} no longer exists. "
                f"Choose another model for {definition.display_name}.",
                status=409,
            )

        if entry.availability == "needs_attention":
            raise DomainError(
                "model_not_usable",
                f"The model {effective_model} failed: {entry.availability_detail}. "
                "Verify it again in AI providers or choose another model.",
                status=409,
            )
        if not (entry.availability == "verified" and entry.tool_calling and entry.streaming):
            raise DomainError(
                "model_not_usable",
                f"The model {effective_model} has not passed verification (tool calls and streaming). "
                "Verify it in AI providers or choose another model.",
                status=409,
            )
        context_window = entry.context_window
        if context_window is None or context_window <= 0:
            raise DomainError(
                "model_not_usable",
                f"The context window of {effective_model} is unknown. Enter it in AI providers.",
                status=409,
            )

        try:
            provider = await self._providers.get_provider_by_id(entry.provider_id)
        except DomainError:
            raise DomainError(
                "model_missing",
                f"The model {effective_model} no longer exists. "
                f"Choose another model for {definition.display_name}.",
                status=409,
            ) from None

        if provider.status == "needs_attention":
            raise DomainError(
                "provider_needs_attention",
                f"{provider.name} needs attention: {provider.status_detail}. Fix it in AI providers.",
                status=409,
            )

        keys = await self._providers.keys_for(provider.id)
        if provider.kind == "hosted" and not keys:
            raise DomainError(
                "provider_has_no_key",
                f"{provider.name} has no key. Add one in AI providers.",
                status=409,
            )

        resolved = ResolvedModel(
            address=entry.address,
            provider_id=provider.id,
            provider_name=provider.name,
            base_url=provider.base_url,
            wire_model_id=entry.wire_model_id,
            context_window=context_window,
            keys=tuple(keys),
        )
        return resolved, max_steps

    async def _check_model_usable(self, address: str) -> ModelEntry:
        entry = await self._providers.require_model(address)
        if entry.availability == "needs_attention":
            raise DomainError(
                "model_not_usable",
                f"The model {address} failed: {entry.availability_detail}. "
                "Verify it again in AI providers or choose another model.",
                status=409,
            )
        if not (entry.availability == "verified" and entry.tool_calling and entry.streaming):
            raise DomainError(
                "model_not_usable",
                f"The model {address} has not passed verification (tool calls and streaming). "
                "Verify it in AI providers or choose another model.",
                status=409,
            )
        context_window = entry.context_window
        if context_window is None or context_window <= 0:
            raise DomainError(
                "model_not_usable",
                f"The context window of {address} is unknown. Enter it in AI providers.",
                status=409,
            )
        return entry

    async def _get_default_model(self, agent: str) -> str | None:
        app_engine = await self._databases.app()
        async with app_engine.connect() as conn:
            res = await conn.scalar(
                select(agent_model_defaults.c.model_address).where(
                    agent_model_defaults.c.agent == agent
                )
            )
            return str(res) if res is not None else None

    async def _get_project_settings(
        self, project_id: str, agent: str, definition: AgentDefinition
    ) -> tuple[str | None, int | None]:
        proj_engine = await self._databases.project(project_id)
        model_key = f"agent.{agent}.model"
        steps_key = f"agent.{agent}.max_steps"
        async with proj_engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(project_settings).where(
                            project_settings.c.key.in_([model_key, steps_key])
                        )
                    )
                )
                .mappings()
                .all()
            )
        model_row = next((r for r in rows if r["key"] == model_key), None)
        steps_row = next((r for r in rows if r["key"] == steps_key), None)

        project_model = str(model_row["value"]) if model_row is not None else None
        if steps_row is not None:
            max_steps_raw: Any = json.loads(str(steps_row["value"]))
            max_steps = int(max_steps_raw) if max_steps_raw is not None else None
        else:
            max_steps = definition.limits.max_steps
        return project_model, max_steps

    async def _compute_model_problem(
        self, definition: AgentDefinition, effective_model: str | None
    ) -> str | None:
        if effective_model is None:
            return (
                f"Choose a model for {definition.display_name} in AI providers, "
                "or in this project's settings."
            )
        entry = await self._providers.get_model(effective_model)
        if entry is None:
            return (
                f"The model {effective_model} no longer exists. "
                f"Choose another model for {definition.display_name}."
            )
        if entry.availability == "needs_attention":
            return (
                f"The model {effective_model} failed: {entry.availability_detail}. "
                "Verify it again in AI providers or choose another model."
            )
        if not (entry.availability == "verified" and entry.tool_calling and entry.streaming):
            return (
                f"The model {effective_model} has not passed verification (tool calls and streaming). "
                "Verify it in AI providers or choose another model."
            )
        if entry.context_window is None or entry.context_window <= 0:
            return f"The context window of {effective_model} is unknown. Enter it in AI providers."
        try:
            provider = await self._providers.get_provider_by_id(entry.provider_id)
        except DomainError:
            return (
                f"The model {effective_model} no longer exists. "
                f"Choose another model for {definition.display_name}."
            )
        if provider.status == "needs_attention":
            return f"{provider.name} needs attention: {provider.status_detail}. Fix it in AI providers."
        if provider.kind == "hosted" and not provider.keys:
            return f"{provider.name} has no key. Add one in AI providers."
        return None
