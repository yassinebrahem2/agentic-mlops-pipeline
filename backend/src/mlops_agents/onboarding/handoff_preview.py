"""The one readable rendering of a handoff payload, used by the kickoff and the handoff card.

Pure and cheap: the Propose tool renders a preview twice per call. A payload never holds a
secret, so nothing here is scrubbed. A missing optional value reads "not set" and an empty
part "none", never nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    CiResults,
    CommandSpec,
    DataFeedRef,
    Feed,
    FileResults,
    HandoffPayload,
    MlflowResults,
    ModelSpec,
    OnCall,
    Profile,
    TableUse,
)
from mlops_agents.tracking.contract import GithubActionsSettings, MlflowSettings

NOT_SET: Final = "not set"
NONE: Final = "none"
_INDENT: Final = "  "


def _opt(value: object) -> str:
    return NOT_SET if value is None else str(value)


def _joined(values: list[str]) -> str:
    return ", ".join(values) if values else NONE


def _list(lines: list[str], indent: str = _INDENT) -> list[str]:
    return [f"{indent}- {line}" for line in lines] if lines else [f"{indent}- {NONE}"]


def _table(use: TableUse | None) -> str:
    return NOT_SET if use is None else f"{use.connection}.{use.table}"


def _cadence(cadence: Cadence) -> str:
    if cadence.cron is not None:
        return f"cron {cadence.cron}"
    return f"every {cadence.every_minutes} minutes"


def _results(results: FileResults | CiResults | MlflowResults) -> str:
    if isinstance(results, FileResults):
        return f"file {results.connection}:{results.path_pattern}"
    if isinstance(results, CiResults):
        return f"CI workflow {results.workflow}"
    fires = NOT_SET if results.fires_when is None else results.fires_when
    return (
        f"MLflow metric {results.metric}, threshold {_opt(results.threshold)}, fires when {fires}"
    )


def _command(spec: CommandSpec) -> str:
    if spec.type == "command":
        how = f"template {spec.template}"
    else:
        inputs = _joined([f"{key}={value}" for key, value in sorted(spec.inputs.items())])
        how = f"workflow {spec.workflow}; inputs {inputs}"
    return (
        f"{spec.kind} ({spec.type}): {how}; placeholders {_joined(list(spec.placeholders))}; "
        f"deploys: {'yes' if spec.deploys else 'no'}; entry files {_joined(spec.entry_files)}"
    )


def _profile(profile: Profile) -> list[str]:
    groups = [
        f"{g.column} ({_joined(g.values) if g.values is not None else 'all values'})"
        for g in profile.groups
    ]
    return [
        f"objective: {profile.objective}",
        f"problem type: {profile.problem_type}",
        f"target column: {profile.target_column}",
        f"positive class: {_opt(profile.positive_class)}",
        f"primary metric: {profile.primary_metric} (why: {profile.metric_rationale})",
        f"operating threshold: {_opt(profile.operating_threshold)}",
        f"label delay: {profile.label_delay_days} days",
        f"label maturity: {profile.label_maturity_days} days",
        f"serving mode: {profile.serving_mode}",
        f"error costs: {profile.error_costs}",
        f"cost ratio: {_opt(profile.cost_ratio)}",
        f"groups: {_joined(groups)}",
    ]


def _model(spec: ModelSpec) -> list[str]:
    environments = [f"{alias} -> {env}" for alias, env in spec.environments.items()]
    signal = spec.production_signal
    return [
        f"Model {spec.name}",
        f"{_INDENT}profile:",
        *[f"{_INDENT * 2}{line}" for line in _profile(spec.profile)],
        f"{_INDENT}predictions: {_table(spec.predictions)}",
        f"{_INDENT}labels: {_table(spec.labels)}",
        f"{_INDENT}commands:",
        *_list([_command(c) for c in spec.commands], _INDENT * 2),
        f"{_INDENT}promotion: {spec.promotion}",
        f"{_INDENT}environments (tracker alias -> environment): {_joined(environments)}",
        f"{_INDENT}production signal: "
        + (NOT_SET if signal is None else f"{signal.kind} {signal.ref}"),
    ]


def _models(payload: HandoffPayload) -> str:
    lines = ["Models"]
    for spec in payload.models:
        lines.extend(f"{_INDENT}{line}" for line in _model(spec))
    if not payload.models:
        lines.append(f"{_INDENT}- {NONE}")
    return "\n".join(lines)


def _commands(payload: HandoffPayload) -> str:
    return "\n".join(["Project commands", *_list([_command(c) for c in payload.commands])])


def _data_roles(payload: HandoffPayload) -> str:
    roles = [
        f"{r.connection}.{r.table}: {r.role}; id column {_opt(r.id_column)}; "
        f"time column {_opt(r.time_column)}"
        for r in payload.data_roles
    ]
    return "\n".join(["Data roles", *_list(roles)])


def _settings(settings: MlflowSettings | GithubActionsSettings) -> str:
    if isinstance(settings, MlflowSettings):
        return f"url {settings.url}"
    return f"repository {settings.repository}; workflows {_joined(settings.workflows)}"


def _tracking(payload: HandoffPayload) -> str:
    sources = [f"{s.type}: {_settings(s.settings)}; auth {s.auth}" for s in payload.tracking]
    return "\n".join(["Tracking sources", *_list(sources)])


def _pipeline(payload: HandoffPayload) -> str:
    stages = [
        f"{s.name}: entry {s.entry}; inputs {_joined(s.inputs)}; outputs {_joined(s.outputs)}"
        for s in payload.pipeline
    ]
    edges = _joined([f"{e.from_stage} -> {e.to_stage}" for e in payload.edges])
    return "\n".join(
        ["Pipeline", f"{_INDENT}stages:", *_list(stages, _INDENT * 2), f"{_INDENT}edges: {edges}"]
    )


def _feed(feed: Feed) -> str:
    ref = feed.ref
    if isinstance(ref, DataFeedRef):
        where = f"table {ref.connection}.{ref.table}"
    elif ref.ci_workflow is not None:
        where = f"CI workflow {ref.ci_workflow}"
    else:
        where = _results(ref.file) if ref.file is not None else NOT_SET
    return (
        f"{feed.name} ({feed.kind}): {where}; {_cadence(feed.cadence)}; "
        f"grace {feed.grace_minutes} minutes; time zone {feed.timezone}"
    )


def _on_call(entry: OnCall) -> str:
    contacts = _joined([f"{c.channel} {c.address}" for c in entry.contacts])
    hours = entry.hours
    return (
        f"{entry.name} (escalation order {entry.escalation_order}): {contacts}; "
        f"hours {', '.join(hours.days)} {hours.start}-{hours.end}; time zone {entry.timezone}"
    )


def _check(check: CheckSpec) -> str:
    return (
        f"{check.check_id} {check.name} ({check.category}): {_cadence(check.cadence)}; "
        f"time zone {check.timezone}; results {_results(check.results)}"
    )


def _operations(payload: HandoffPayload) -> str:
    ops = payload.operations
    scheduler = (
        NOT_SET
        if ops.scheduler is None
        else f"{ops.scheduler.name}, configured in {ops.scheduler.config_location}"
    )
    return "\n".join(
        [
            "Operations",
            f"{_INDENT}scheduler: {scheduler}",
            f"{_INDENT}feeds:",
            *_list([_feed(f) for f in ops.feeds], _INDENT * 2),
            f"{_INDENT}on call:",
            *_list([_on_call(o) for o in ops.on_call], _INDENT * 2),
            f"{_INDENT}checks:",
            *_list([_check(c) for c in ops.checks], _INDENT * 2),
            f"{_INDENT}results location: {_results(ops.results_location)}",
        ]
    )


def _gaps(payload: HandoffPayload) -> str:
    gaps = [f"{g.what} (next step: {g.next_step})" for g in payload.gaps]
    return "\n".join(["Gaps", *_list(gaps)])


def _grants(payload: HandoffPayload) -> str:
    grants = payload.grants
    return "\n".join(
        [
            "Grants",
            f"{_INDENT}network: {'on' if grants.network else 'off'}",
            f"{_INDENT}permission level: {grants.permission_level}",
        ]
    )


# The fixed order; the keys are the payload's own part names, as ReadHandoff's sections are.
_SECTIONS: Final[tuple[tuple[str, Callable[[HandoffPayload], str]], ...]] = (
    ("models", _models),
    ("commands", _commands),
    ("data_roles", _data_roles),
    ("tracking", _tracking),
    ("pipeline", _pipeline),
    ("operations", _operations),
    ("gaps", _gaps),
    ("grants", _grants),
)


def sections(payload: HandoffPayload) -> dict[str, str]:
    """Each part of the handoff rendered on its own, in the fixed order."""
    return {key: render_section(payload) for key, render_section in _SECTIONS}


def render(payload: HandoffPayload) -> str:
    """The whole handoff: the sections joined in the fixed order."""
    return "\n\n".join(sections(payload).values())
