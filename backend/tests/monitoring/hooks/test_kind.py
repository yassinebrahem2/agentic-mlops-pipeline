"""Unit tests for HooksKind proposal kind."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.contract import (
    HookProposal,
    HooksPayload,
    HookView,
)
from mlops_agents.monitoring.hooks.kind import HooksKind
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.contract import HOOKS_KIND, ProposalKind
from mlops_agents.proposals.payloads import CommandSpec
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
