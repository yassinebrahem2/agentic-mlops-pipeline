"""Training HookLifecycle implementation (Task 3, #488)."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookLifecycle,
    HookRefused,
    HookResult,
    HookView,
    PlaceholderResolver,
    RunContext,
)
from mlops_agents.monitoring.hooks.placeholders import TrackerPlaceholderResolver
from mlops_agents.training.contract import TrainingWindow

if TYPE_CHECKING:
    from mlops_agents.events.contract import EventLog
    from mlops_agents.providers.failover import Clock
    from mlops_agents.training.fixes import SqliteFixes
    from mlops_agents.training.requests import SqliteTraining

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._:+-]{1,128}$")


class TrainingLifecycle(HookLifecycle):
    """Lifecycle handler resolving placeholders, guarding training runs, and tracking trq rows."""

    def __init__(
        self,
        training: SqliteTraining,
        fixes: SqliteFixes,
        resolver: PlaceholderResolver,
        clock: Clock,
        events: EventLog,
    ) -> None:
        self._training = training
        self._fixes = fixes
        self._resolver = resolver
        self._clock = clock
        self._events = events

    async def before(self, project_id: str, hook: HookView, call: HookCall) -> HookCall:
        is_training = hook.spec.kind in ("train", "evaluate")

        if hook.model is not None and not _SAFE_NAME_RE.match(hook.model):
            raise HookRefused(f"Invalid model name format: {hook.model!r}")

        candidate_version = call.params.get("candidate_version")
        if candidate_version is not None and not _SAFE_NAME_RE.match(candidate_version):
            raise HookRefused(f"Invalid candidate_version format: {candidate_version!r}")

        threshold: float | None = None
        if "threshold" in call.params:
            try:
                threshold = float(call.params["threshold"])
                if not math.isfinite(threshold):
                    raise ValueError("non-finite threshold")
            except (ValueError, TypeError) as err:
                raise HookRefused(
                    f"Invalid threshold format: {call.params['threshold']!r}"
                ) from err

        window: TrainingWindow | None = None
        window_start_str = call.params.get("window_start")
        window_end_str = call.params.get("window_end")

        if window_start_str is not None or window_end_str is not None:
            if window_start_str is None or window_end_str is None:
                raise HookRefused("Both window_start and window_end must be provided")
            try:
                start_dt = datetime.fromisoformat(window_start_str)
                end_dt = datetime.fromisoformat(window_end_str)
            except Exception as err:
                raise HookRefused(f"Invalid window date format: {err}") from err

            start_dt = (
                start_dt.replace(tzinfo=UTC)
                if start_dt.tzinfo is None
                else start_dt.astimezone(UTC)
            )
            end_dt = end_dt.replace(tzinfo=UTC) if end_dt.tzinfo is None else end_dt.astimezone(UTC)

            window = TrainingWindow(start=start_dt, end=end_dt)

        if is_training:
            # 1. Take window from call params
            if window is None:
                raise HookRefused("Window is required for training/evaluation hooks")

            if hook.model is None:
                raise HookRefused("Training/evaluation hook requires a model")

            # 2. Run fixes.guards
            guard_result = await self._fixes.guards(
                project_id,
                hook.model,
                window,
                policy_id=call.policy_id,
            )
            if not guard_result.ok:
                refusal_lines = [f"{r.code}: {r.detail}" for r in guard_result.refusals]
                raise HookRefused("Guards refused training run:\n" + "\n".join(refusal_lines))

            # 3. Generate request ID
            request_id = new_id("trq")

            # 4. Build context
            ctx = RunContext(
                request_id=request_id,
                model=hook.model,
                window=window,
                mode=hook.spec.kind,  # type: ignore[arg-type]
                candidate_version=candidate_version,
                threshold=threshold,
            )

            # 5. Resolve placeholders
            if isinstance(self._resolver, TrackerPlaceholderResolver):
                resolved = await self._resolver.resolve(hook, ctx, project_id=project_id)
            else:
                resolved = await self._resolver.resolve(hook, ctx)

            # 6. Insert training request row
            await self._training.create_request(
                project_id=project_id,
                request_id=request_id,
                model=hook.model,
                hook_id=hook.id,
                mode=hook.spec.kind,  # type: ignore[arg-type]
                status="started",
                fix_id=call.fix_id,
                policy_id=call.policy_id,
                window=window,
            )

            # 7. Record event training.requested
            await self._events.record(
                project_id,
                source="training",
                type="training.requested",
                payload={
                    "request_id": request_id,
                    "hook_id": hook.id,
                    "model": hook.model,
                    "fix_id": call.fix_id,
                },
                subject_id=call.fix_id if call.fix_id is not None else hook.id,
            )

            # 8. Move fix from chosen to running if fix_id provided
            if call.fix_id is not None:
                await self._fixes.mark_running(project_id, call.fix_id)

            # 9. Return updated HookCall
            new_params = dict(call.params)
            new_params.update(resolved)
            new_params["request_id"] = request_id
            return call.model_copy(update={"params": new_params})

        else:
            new_params = dict(call.params)
            new_params.pop("request_id", None)
            ctx = RunContext(
                model=hook.model or "",
                window=window,
                candidate_version=candidate_version,
                threshold=threshold,
            )
            if isinstance(self._resolver, TrackerPlaceholderResolver):
                resolved = await self._resolver.resolve(hook, ctx, project_id=project_id)
            else:
                resolved = await self._resolver.resolve(hook, ctx)
            new_params.update(resolved)
            return call.model_copy(update={"params": new_params})

    async def after(
        self,
        project_id: str,
        hook: HookView,
        call: HookCall,
        result: HookResult,
    ) -> None:
        if hook.spec.kind not in ("train", "evaluate"):
            return
        request_id = result.request_id or call.params.get("request_id")
        if not result.ok and request_id is not None:
            await self._training.set_status(project_id, request_id, "failed")
