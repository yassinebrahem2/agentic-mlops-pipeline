"""Alias-history verifier for linked training runs (Task 7, #492)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mlops_agents.monitoring.hooks.contract import (
    AliasCheck,
    Hooks,
    HookVerifier,
    PinCheck,
)
from mlops_agents.monitoring.hooks.pins import verify_hook_pins
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.tracking.constants import PRODUCTION_ENVIRONMENT
from mlops_agents.tracking.contract import (
    DeploymentRow,
    ModelVersionRow,
    Tracking,
    TrackingQuery,
)
from mlops_agents.training.contract import TrainingWindow
from mlops_agents.training.requests import SqliteTraining

if TYPE_CHECKING:
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)


class HookAliasVerifier(HookVerifier):
    """Verifies whether a completed run moved a production alias and checks pins."""

    def __init__(
        self,
        tracking: Tracking,
        handoffs: Handoffs,
        training: SqliteTraining,
        *,
        hooks: Hooks | None = None,
        workspace: WorkspaceService | None = None,
        git: GitRunner | None = None,
    ) -> None:
        self._tracking = tracking
        self._handoffs = handoffs
        self._training = training
        self._hooks = hooks
        self._workspace = workspace
        self._git = git

    async def verify_pins(self, project_id: str, hook_id: str) -> PinCheck:
        if self._hooks is None or self._workspace is None or self._git is None:
            return PinCheck(ok=True, changed=[])
        return await verify_hook_pins(self._hooks, self._workspace, self._git, project_id, hook_id)

    async def suspend_hook(self, project_id: str, hook_id: str, reason: str) -> None:
        if self._hooks is not None:
            await self._hooks.suspend(project_id, hook_id, reason)
        await self._training.suspend_policies_for_hook(project_id, hook_id, reason)

    async def check_run(
        self,
        project_id: str,
        hook_id: str,
        request_id: str,
        window: TrainingWindow,
    ) -> AliasCheck:
        req = await self._training.request(project_id, request_id)
        if req is None or req.run_key is None:
            return AliasCheck(moved=False, detail="No linked run key found")

        handoff = await self._handoffs.current(project_id)
        if handoff is None:
            return AliasCheck(moved=False, detail="No current handoff")

        model_spec = next((m for m in handoff.payload.models if m.name == req.model), None)
        if model_spec is None:
            return AliasCheck(moved=False, detail=f"Model {req.model!r} not in handoff")

        if model_spec.production_signal is None:
            return AliasCheck(moved=False, detail="Model has no production signal")

        prod_aliases: set[str] = {
            alias for alias, env in model_spec.environments.items() if env == PRODUCTION_ENVIRONMENT
        }
        if model_spec.production_signal.kind == "tracker_alias":
            prod_aliases.add(model_spec.production_signal.ref)

        if not prod_aliases:
            return AliasCheck(moved=False, detail="No production aliases configured")

        # 1. Query versions registered by this run
        vers_page = await self._tracking.query(
            project_id,
            TrackingQuery(what="versions", model=req.model, run_key=req.run_key),
        )
        run_version_rows = [r for r in vers_page.rows if isinstance(r, ModelVersionRow)]
        run_version_numbers = {r.version for r in run_version_rows}

        for v_row in run_version_rows:
            for alias in v_row.aliases:
                if alias in prod_aliases:
                    return AliasCheck(
                        moved=True,
                        detail=(
                            f"Production alias '{alias}' moved to version '{v_row.version}' "
                            f"of run '{req.run_key}'"
                        ),
                    )

        # 2. Query deployments during/after run started
        dep_page = await self._tracking.query(
            project_id,
            TrackingQuery(what="deployments", model=req.model, since=req.started),
        )
        for d in dep_page.rows:
            if isinstance(d, DeploymentRow) and d.alias in prod_aliases and d.to_version:
                if d.to_version in run_version_numbers:
                    return AliasCheck(
                        moved=True,
                        detail=(
                            f"Production alias '{d.alias}' moved to version '{d.to_version}' "
                            f"of run '{req.run_key}'"
                        ),
                    )
                # Fallback: check version row specifically for d.to_version
                spec_vers = await self._tracking.query(
                    project_id,
                    TrackingQuery(what="versions", model=req.model, version=d.to_version),
                )
                for sv in spec_vers.rows:
                    if isinstance(sv, ModelVersionRow) and sv.run_key == req.run_key:
                        return AliasCheck(
                            moved=True,
                            detail=(
                                f"Production alias '{d.alias}' moved to version '{d.to_version}' "
                                f"of run '{req.run_key}'"
                            ),
                        )

        return AliasCheck(moved=False, detail="No production alias move")
