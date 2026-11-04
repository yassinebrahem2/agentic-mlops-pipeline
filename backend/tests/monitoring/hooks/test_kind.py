"""Unit tests for HooksKind proposal kind."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from sqlalchemy import insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.contract import (
    HookProposal,
    HooksPayload,
    HookView,
)
from mlops_agents.monitoring.hooks.kind import HooksKind
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.contract import HOOKS_KIND, ProposalCheck, ProposalKind
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import file_claims, sessions, tasks
from tests.proposals.samples import command


def _sample_spec(
    kind: str = "train",
    cmd_type: str = "command",
    template: str | None = None,
    workflow: str | None = None,
    deploys: bool = False,
    placeholders: list[str] | None = None,
    entry_files: list[str] | None = None,
) -> CommandSpec:
    tmpl = template if cmd_type == "command" else None
    if tmpl is None and cmd_type == "command":
        tmpl = "python train.py --model {model}"
    wf = workflow if cmd_type == "ci_dispatch" else None
    if placeholders is not None:
        pls = placeholders
    elif tmpl and "{model}" in tmpl:
        pls = ["model"]
    else:
        pls = []
    return CommandSpec.model_validate(
        command(
            kind=kind,
            type=cmd_type,
            template=tmpl,
            workflow=wf,
            deploys=deploys,
            placeholders=pls,
            entry_files=entry_files if entry_files is not None else [],
        )
    )


def test_hooks_kind_conforms_to_proposal_kind(service: HooksService) -> None:
    kind: ProposalKind = HooksKind(hooks=service)
    assert kind.name == HOOKS_KIND
    assert kind.name == "hooks"
    assert kind.payload_model is HooksPayload
    for method in ("key", "preview", "warnings", "credentials_needed", "apply"):
        assert callable(getattr(kind, method))
    assert isinstance(kind, ProposalCheck)


def test_key(service: HooksService) -> None:
    kind = HooksKind(hooks=service)
    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="train-hook",
                model="churn",
                spec=_sample_spec(),
                why="Train model on new data",
            )
        ]
    )
    assert kind.key(payload) == "project"


def test_credentials_needed(service: HooksService) -> None:
    kind = HooksKind(hooks=service)
    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="train-hook",
                model=None,
                spec=_sample_spec(),
                why="Project wide hook",
            )
        ]
    )
    assert kind.credentials_needed(payload) == []


def test_preview(service: HooksService) -> None:
    kind = HooksKind(hooks=service)
    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="train-hook",
                model="churn",
                spec=_sample_spec(kind="train", cmd_type="command"),
                why="Train model on new data",
            ),
            HookProposal(
                name="ci-eval",
                model=None,
                spec=_sample_spec(kind="evaluate", cmd_type="ci_dispatch", workflow="eval.yml"),
                why="Evaluate pipeline via CI",
            ),
        ]
    )
    prev = kind.preview(payload)
    assert prev.kind == "text"
    assert prev.title == "Hooks"
    assert "train-hook (model: churn) (train, command): why: Train model on new data" in prev.body
    assert "ci-eval (evaluate, ci_dispatch): why: Evaluate pipeline via CI" in prev.body


def test_warnings_deploys_flag(service: HooksService) -> None:
    kind = HooksKind(hooks=service)
    # No deploys hooks -> no warnings
    no_deploy_payload = HooksPayload(
        hooks=[
            HookProposal(
                name="train-hook",
                model="churn",
                spec=_sample_spec(deploys=False),
                why="Safe training",
            ),
            HookProposal(
                name="eval-hook",
                model=None,
                spec=_sample_spec(deploys=False),
                why="Safe evaluation",
            ),
        ]
    )
    assert kind.warnings(no_deploy_payload) == []

    # Deploys hooks -> produces warnings naming the hook entry
    deploy_payload = HooksPayload(
        hooks=[
            HookProposal(
                name="deploy-hook",
                model="churn",
                spec=_sample_spec(kind="deploy", deploys=True),
                why="Deploys candidate model",
            ),
            HookProposal(
                name="eval-hook",
                model=None,
                spec=_sample_spec(deploys=False),
                why="Safe eval",
            ),
            HookProposal(
                name="global-deploy",
                model=None,
                spec=_sample_spec(kind="deploy", deploys=True),
                why="Project-wide deploy",
            ),
        ]
    )
    warns = kind.warnings(deploy_payload)
    assert len(warns) == 2
    assert "Hook 'deploy-hook' (model: churn) deploys." in warns
    assert "Hook 'global-deploy' deploys." in warns


@pytest.mark.asyncio
async def test_apply_and_idempotent(service: HooksService) -> None:
    kind = HooksKind(hooks=service)
    project_id = new_id("prj")
    proposal_id = new_id("prp")

    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="train-hook",
                model="churn",
                spec=_sample_spec(kind="train", cmd_type="command"),
                why="Train model",
            ),
            HookProposal(
                name="eval-hook",
                model=None,
                spec=_sample_spec(kind="evaluate", cmd_type="ci_dispatch", workflow="eval.yml"),
                why="Global eval",
            ),
        ]
    )

    # First apply
    res1 = await kind.apply(project_id, proposal_id, payload)
    assert res1.applied == ["hook:churn:train-hook", "hook:eval-hook"]
    assert res1.failed == {}

    # Verify hooks exist and source is "proposal"
    hooks_list = await service.list(project_id)
    assert len(hooks_list) == 2
    by_name = {h.name: h for h in hooks_list}
    assert by_name["train-hook"].source == "proposal"
    assert by_name["train-hook"].model == "churn"
    assert by_name["eval-hook"].source == "proposal"
    assert by_name["eval-hook"].model is None

    # Second apply: idempotent, returns same applied parts and no failures
    res2 = await kind.apply(project_id, proposal_id, payload)
    assert res2.applied == ["hook:churn:train-hook", "hook:eval-hook"]
    assert res2.failed == {}

    # Hook count remains 2
    hooks_after = await service.list(project_id)
    assert len(hooks_after) == 2


@pytest.mark.asyncio
async def test_apply_captures_failure() -> None:
    class FailingHooks:
        async def register(
            self,
            project_id: str,
            spec: CommandSpec,
            name: str,
            model: str | None,
            source: str,
        ) -> HookView:
            if name == "fail-hook":
                raise DomainError("hook_invalid", "Invalid hook definition")
            if name == "crash-hook":
                raise RuntimeError("Database connection lost")
            raise NotImplementedError

    failing_service = FailingHooks()
    # pyright: ignore[reportArgumentType]
    kind = HooksKind(hooks=failing_service)  # type: ignore[arg-type]

    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="fail-hook",
                model="churn",
                spec=_sample_spec(),
                why="Will fail with domain error",
            ),
            HookProposal(
                name="crash-hook",
                model=None,
                spec=_sample_spec(),
                why="Will fail with runtime error",
            ),
        ]
    )

    res = await kind.apply("prj_1", "prp_1", payload)
    assert res.applied == []
    assert res.failed["hook:churn:fail-hook"] == "Invalid hook definition"
    assert "Database connection lost" in res.failed["hook:crash-hook"]


def test_type_error_on_invalid_payload(service: HooksService) -> None:
    kind = HooksKind(hooks=service)

    class OtherPayload(BaseModel):
        foo: str = "bar"

    bad = OtherPayload()
    with pytest.raises(TypeError, match="the hooks kind got a OtherPayload"):
        kind.key(bad)
    with pytest.raises(TypeError, match="the hooks kind got a OtherPayload"):
        kind.preview(bad)
    with pytest.raises(TypeError, match="the hooks kind got a OtherPayload"):
        kind.warnings(bad)


async def _insert_task(
    databases: Databases,
    project_id: str,
    task_id: str,
    status: str = "active",
    pr_number: int | None = None,
    claimed_files: list[str] | None = None,
) -> None:
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            sqlite_insert(sessions)
            .values(
                id="ses_1",
                agent="coder",
                created_at=datetime.now(UTC).isoformat(),
            )
            .on_conflict_do_nothing()
        )
        await conn.execute(
            insert(tasks).values(
                id=task_id,
                session_id="ses_1",
                agent="coder",
                title="test task",
                branch="feature/test",
                worktree="wt_1",
                base_ref="main",
                base_sha="abc1234",
                status=status,
                pr_number=pr_number,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        if claimed_files:
            for path in claimed_files:
                await conn.execute(
                    insert(file_claims).values(
                        task_id=task_id,
                        path=path,
                    )
                )


@pytest.mark.asyncio
async def test_check_passes_when_no_databases(service: HooksService) -> None:
    kind = HooksKind(hooks=service)
    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="ci-eval",
                model=None,
                spec=_sample_spec(kind="evaluate", cmd_type="ci_dispatch", workflow="eval.yml"),
                why="Evaluate pipeline via CI",
            )
        ]
    )
    refusals = await kind.check("prj_1", payload)
    assert refusals == []


@pytest.mark.asyncio
async def test_check_passes_when_no_claims(service: HooksService, databases: Databases) -> None:
    kind = HooksKind(hooks=service, databases=databases)
    project_id = new_id("prj")
    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="ci-eval",
                model=None,
                spec=_sample_spec(kind="evaluate", cmd_type="ci_dispatch", workflow="eval.yml"),
                why="Evaluate pipeline via CI",
            )
        ]
    )
    refusals = await kind.check(project_id, payload)
    assert refusals == []


@pytest.mark.asyncio
async def test_check_refuses_when_entry_file_claimed_by_open_task(
    service: HooksService, databases: Databases
) -> None:
    kind = HooksKind(hooks=service, databases=databases)
    project_id = new_id("prj")

    await _insert_task(
        databases,
        project_id,
        task_id="task_1",
        status="active",
        pr_number=17,
        claimed_files=["workflows/hold.py"],
    )

    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="hold-predictions",
                model=None,
                spec=_sample_spec(
                    kind="evaluate",
                    cmd_type="ci_dispatch",
                    workflow="workflows/hold.py",
                ),
                why="Hold predictions on drift",
            )
        ]
    )

    refusals = await kind.check(project_id, payload)
    assert refusals == [
        "workflows/hold.py is being changed in task task_1 (PR #17); propose this hook after that PR merges"
    ]


@pytest.mark.asyncio
async def test_check_passes_when_task_is_not_active_or_in_review(
    service: HooksService, databases: Databases
) -> None:
    kind = HooksKind(hooks=service, databases=databases)
    project_id = new_id("prj")

    await _insert_task(
        databases,
        project_id,
        task_id="task_merged",
        status="merged",
        pr_number=18,
        claimed_files=["workflows/hold.py"],
    )
    await _insert_task(
        databases,
        project_id,
        task_id="task_closed",
        status="closed",
        pr_number=19,
        claimed_files=["workflows/hold.py"],
    )

    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="hold-predictions",
                model=None,
                spec=_sample_spec(
                    kind="evaluate",
                    cmd_type="ci_dispatch",
                    workflow="workflows/hold.py",
                ),
                why="Hold predictions on drift",
            )
        ]
    )

    refusals = await kind.check(project_id, payload)
    assert refusals == []


@pytest.mark.asyncio
async def test_check_refuses_when_task_is_in_review(
    service: HooksService, databases: Databases
) -> None:
    kind = HooksKind(hooks=service, databases=databases)
    project_id = new_id("prj")

    await _insert_task(
        databases,
        project_id,
        task_id="task_review",
        status="in_review",
        pr_number=20,
        claimed_files=["workflows/hold.py"],
    )

    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="hold-predictions",
                model=None,
                spec=_sample_spec(
                    kind="evaluate",
                    cmd_type="ci_dispatch",
                    workflow="workflows/hold.py",
                ),
                why="Hold predictions on drift",
            )
        ]
    )

    refusals = await kind.check(project_id, payload)
    assert refusals == [
        "workflows/hold.py is being changed in task task_review (PR #20); propose this hook after that PR merges"
    ]


@pytest.mark.asyncio
async def test_check_with_entry_files_and_no_pr(
    service: HooksService, databases: Databases
) -> None:
    kind = HooksKind(hooks=service, databases=databases)
    project_id = new_id("prj")

    await _insert_task(
        databases,
        project_id,
        task_id="task_2",
        status="active",
        pr_number=None,
        claimed_files=["scripts/train.py"],
    )

    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="train-hook",
                model="churn",
                spec=_sample_spec(
                    kind="train",
                    cmd_type="command",
                    entry_files=["scripts/train.py"],
                ),
                why="Train model on new data",
            )
        ]
    )

    refusals = await kind.check(project_id, payload)
    assert refusals == [
        "scripts/train.py is being changed in task task_2 (no PR); propose this hook after that PR merges"
    ]


@pytest.mark.asyncio
async def test_check_multiple_hooks_and_order_preserved(
    service: HooksService, databases: Databases
) -> None:
    kind = HooksKind(hooks=service, databases=databases)
    project_id = new_id("prj")

    await _insert_task(
        databases,
        project_id,
        task_id="task_b",
        status="active",
        pr_number=102,
        claimed_files=["scripts/evaluate.py"],
    )
    await _insert_task(
        databases,
        project_id,
        task_id="task_a",
        status="active",
        pr_number=101,
        claimed_files=["workflows/pipeline.yml"],
    )

    payload = HooksPayload(
        hooks=[
            HookProposal(
                name="hook-1",
                model=None,
                spec=_sample_spec(
                    kind="evaluate",
                    cmd_type="ci_dispatch",
                    workflow="workflows/pipeline.yml",
                ),
                why="Run workflow",
            ),
            HookProposal(
                name="hook-2",
                model=None,
                spec=_sample_spec(
                    kind="evaluate",
                    cmd_type="command",
                    entry_files=["scripts/evaluate.py"],
                ),
                why="Run eval",
            ),
        ]
    )

    refusals = await kind.check(project_id, payload)
    assert refusals == [
        "workflows/pipeline.yml is being changed in task task_a (PR #101); propose this hook after that PR merges",
        "scripts/evaluate.py is being changed in task task_b (PR #102); propose this hook after that PR merges",
    ]
