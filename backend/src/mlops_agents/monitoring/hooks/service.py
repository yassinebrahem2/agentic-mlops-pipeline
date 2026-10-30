"""HooksService: action hooks store and runs, runner/lifecycle/pinner seams."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.clock import to_iso
from mlops_agents.credentials.project_secrets import ProjectSecrets, hook_ref
from mlops_agents.credentials.scrub import error_text, scrub_url_secrets
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.constants import HOOK_DETAIL_MAX_CHARS
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookLifecycle,
    HookPinner,
    HookRefused,
    HookResult,
    HookRunner,
    HookRunView,
    HookStatus,
    HookView,
    PinnedFile,
)
from mlops_agents.monitoring.hooks.store import HooksStore, make_hook_view
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases


class HooksService:
    def __init__(
        self,
        databases: Databases,
        secrets: ProjectSecrets,
        events: EventLog,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._secrets = secrets
        self._events = events
        self._clock = clock
        self._store = HooksStore(databases)
        self._runners: dict[str, HookRunner] = {}
        self._lifecycles: list[HookLifecycle] = []
        self._pinner: HookPinner | None = None

    def add_runner(self, runner: HookRunner) -> None:
        self._runners[runner.type] = runner

    def add_lifecycle(self, lifecycle: HookLifecycle) -> None:
        self._lifecycles.append(lifecycle)

    def set_pinner(self, pinner: HookPinner) -> None:
        self._pinner = pinner

    async def list(self, project_id: str, model: str | None = None) -> list[HookView]:
        raw_hooks = await self._store.list(project_id, model=model)
        result: list[HookView] = []
        for row, pins in raw_hooks:
            hook_id = str(row["id"])
            credential_set = await self._secrets.is_set(hook_ref(project_id, hook_id))
            result.append(make_hook_view(row, pins, credential_set))
        return result

    async def get(self, project_id: str, hook_id: str) -> HookView | None:
        res = await self._store.get(project_id, hook_id)
        if res is None:
            return None
        row, pins = res
        credential_set = await self._secrets.is_set(hook_ref(project_id, hook_id))
        return make_hook_view(row, pins, credential_set)

    async def register(
        self,
        project_id: str,
        spec: CommandSpec,
        name: str,
        model: str | None,
        source: Literal["handoff", "proposal"],
    ) -> HookView:
        existing = await self._store.get_by_model_name(project_id, model, name)
        if existing is not None:
            row, pins = existing
            hook_id = str(row["id"])
            credential_set = await self._secrets.is_set(hook_ref(project_id, hook_id))
            approved_commit: str | None = None
            new_pins: list[PinnedFile] | None = None
            if self._pinner is not None:
                current_view = make_hook_view(
                    {**row, "spec": spec.model_dump()},
                    pins,
                    credential_set,
                )
                commit, pins_result = await self._pinner.pins(project_id, current_view)
                approved_commit = commit
                new_pins = pins_result
            await self._store.update_hook_spec(
                project_id,
                hook_id=hook_id,
                spec=spec.model_dump_json(),
                approved_commit=approved_commit,
                pins=new_pins,
            )
            updated = await self.get(project_id, hook_id)
            if updated is None:
                raise RuntimeError(f"Hook {hook_id!r} not found after update")
            return updated

        hook_id = new_id("hok")
        status: HookStatus = "active"
        created = to_iso(self._clock.now())
        approved_commit = None
        pins = []
        if self._pinner is not None:
            draft_view = HookView(
                id=hook_id,
                name=name,
                model=model,
                spec=spec,
                status=status,
                suspended_reason=None,
                credential_set=False,
                pins=[],
                approved_commit=None,
                deploys_verified=None,
                source=source,
                created=self._clock.now(),
            )
            commit, pins_result = await self._pinner.pins(project_id, draft_view)
            approved_commit = commit
            pins = pins_result

        row = {
            "id": hook_id,
            "name": name,
            "model": model,
            "spec": spec.model_dump_json(),
            "status": status,
            "suspended_reason": None,
            "approved_commit": approved_commit,
            "deploys_verified": None,
            "source": source,
            "created": created,
        }
        await self._store.insert_hook(project_id, row, pins)
        created_view = await self.get(project_id, hook_id)
        if created_view is None:
            raise RuntimeError(f"Hook {hook_id!r} not found after insert")
        return created_view

    async def set_enabled(self, project_id: str, hook_id: str, enabled: bool) -> HookView:
        hook = await self.get(project_id, hook_id)
        if hook is None:
            raise KeyError(f"Hook {hook_id!r} not found in project {project_id!r}")
        if enabled:
            if hook.status != "suspended":
                await self._store.update_hook_status(
                    project_id, hook_id, "active", suspended_reason=None
                )
        else:
            await self._store.update_hook_status(
                project_id, hook_id, "disabled", suspended_reason=hook.suspended_reason
            )
        updated = await self.get(project_id, hook_id)
        if updated is None:
            raise RuntimeError(f"Hook {hook_id!r} not found after update")
        return updated

    async def set_credential(self, project_id: str, hook_id: str, credential: str) -> HookView:
        hook = await self.get(project_id, hook_id)
        if hook is None:
            raise KeyError(f"Hook {hook_id!r} not found in project {project_id!r}")
        await self._secrets.put(hook_ref(project_id, hook_id), credential)
        updated = await self.get(project_id, hook_id)
        if updated is None:
            raise RuntimeError(f"Hook {hook_id!r} not found after update")
        return updated

    async def delete_credential(self, project_id: str, hook_id: str) -> HookView:
        hook = await self.get(project_id, hook_id)
        if hook is None:
            raise KeyError(f"Hook {hook_id!r} not found in project {project_id!r}")
        await self._secrets.delete(hook_ref(project_id, hook_id))
        updated = await self.get(project_id, hook_id)
        if updated is None:
            raise RuntimeError(f"Hook {hook_id!r} not found after update")
        return updated

    async def suspend(self, project_id: str, hook_id: str, reason: str) -> HookView:
        hook = await self.get(project_id, hook_id)
        if hook is None:
            raise KeyError(f"Hook {hook_id!r} not found in project {project_id!r}")
        await self._store.update_hook_status(
            project_id, hook_id, "suspended", suspended_reason=reason
        )
        await self._events.record(
            project_id,
            source="hooks",
            type="hook.suspended",
            subject_id=hook_id,
            payload={"hook_id": hook_id, "reason": reason},
        )
        updated = await self.get(project_id, hook_id)
        if updated is None:
            raise RuntimeError(f"Hook {hook_id!r} not found after update")
        return updated

    async def set_deploys_verified(self, project_id: str, hook_id: str, verified: bool) -> HookView:
        hook = await self.get(project_id, hook_id)
        if hook is None:
            raise KeyError(f"Hook {hook_id!r} not found in project {project_id!r}")
        await self._store.update_deploys_verified(project_id, hook_id, verified)
        updated = await self.get(project_id, hook_id)
        if updated is None:
            raise RuntimeError(f"Hook {hook_id!r} not found after update")
        return updated

    async def approve(self, project_id: str, hook_id: str, by: str) -> HookView:
        hook = await self.get(project_id, hook_id)
        if hook is None:
            raise KeyError(f"Hook {hook_id!r} not found in project {project_id!r}")
        approved_commit: str | None = None
        pins: list[PinnedFile] | None = None
        if self._pinner is not None:
            commit, pins_result = await self._pinner.pins(project_id, hook)
            approved_commit = commit
            pins = pins_result
        await self._store.approve_hook(project_id, hook_id, approved_commit, pins)
        updated = await self.get(project_id, hook_id)
        if updated is None:
            raise RuntimeError(f"Hook {hook_id!r} not found after update")
        return updated

    async def run(self, project_id: str, call: HookCall, authority: Authority) -> HookResult:
        hook = await self.get(project_id, call.hook_id)
        if hook is None:
            raise KeyError(f"Hook {call.hook_id!r} not found in project {project_id!r}")

        run_id = new_id("hok")
        started_at = self._clock.now()
        subject_id = call.incident_id if call.incident_id is not None else hook.id
        event_payload = {
            "hook_id": hook.id,
            "run_id": run_id,
            "kind": hook.spec.kind,
            "basis": authority.basis,
        }

        run_row = {
            "id": run_id,
            "hook_id": hook.id,
            "kind": hook.spec.kind,
            "model": hook.model,
            "incident_id": call.incident_id,
            "fix_id": call.fix_id,
            "rule_id": call.rule_id,
            "policy_id": call.policy_id,
            "reason": call.reason,
            "basis": authority.basis,
            "basis_ref": authority.ref,
            "evidence": json.dumps(authority.evidence),
            "ok": None,
            "detail": None,
            "request_id": call.params.get("request_id"),
            "started": to_iso(started_at),
            "ended": None,
        }
        await self._store.insert_run(project_id, run_row)

        try:
            if hook.status == "disabled":
                raise HookRefused(f"Hook {hook.id!r} is disabled")

            current_call = call
            for lc in self._lifecycles:
                current_call = await lc.before(project_id, hook, current_call)

            runner = self._runners.get(hook.spec.type)
            if runner is None:
                raise HookRefused(f"No runner registered for type {hook.spec.type!r}")

            try:
                res = await runner.run(project_id, hook, current_call.params, authority)
            except Exception as exc:
                res = HookResult(ok=False, detail=error_text(exc))

            detail = scrub_url_secrets(res.detail)
            if len(detail) > HOOK_DETAIL_MAX_CHARS:
                detail = detail[:HOOK_DETAIL_MAX_CHARS]
            req_id = res.request_id or current_call.params.get("request_id")
            result = HookResult(
                ok=res.ok,
                detail=detail,
                ci_run_url=res.ci_run_url,
                request_id=req_id,
            )

            for lc in self._lifecycles:
                await lc.after(project_id, hook, current_call, result)

            ended_at = self._clock.now()
            await self._store.update_run_finish(
                project_id,
                run_id=run_id,
                ok=result.ok,
                detail=result.detail,
                request_id=result.request_id,
                ended=to_iso(ended_at),
            )
            await self._events.record(
                project_id,
                source="hooks",
                type="hook.ran" if result.ok else "hook.failed",
                subject_id=subject_id,
                payload=event_payload,
            )
            return result

        except HookRefused as err:
            err_detail = scrub_url_secrets(str(err))
            if len(err_detail) > HOOK_DETAIL_MAX_CHARS:
                err_detail = err_detail[:HOOK_DETAIL_MAX_CHARS]
            ended_at = self._clock.now()
            await self._store.update_run_finish(
                project_id,
                run_id=run_id,
                ok=False,
                detail=err_detail,
                request_id=call.params.get("request_id"),
                ended=to_iso(ended_at),
            )
            await self._events.record(
                project_id,
                source="hooks",
                type="hook.failed",
                subject_id=subject_id,
                payload=event_payload,
            )
            raise

    async def runs(
        self,
        project_id: str,
        *,
        kinds: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[HookRunView]:
        return await self._store.list_runs(project_id, kinds=kinds, since=since, limit=limit)
