"""The stage 2 contract modules import cleanly (no cycles) and expose the I8 to I12 names."""

import importlib
import inspect

import pytest

MODULES = [
    "mlops_agents.agents.policy.table",
    "mlops_agents.events.contract",
    "mlops_agents.agents.tasks.contract",
    "mlops_agents.sandbox.contract",
    "mlops_agents.agents.approvals.contract",
    "mlops_agents.agents.messaging.contract",
    "mlops_agents.agents.runtime.hooks",
]


@pytest.mark.parametrize("module", MODULES)
def test_each_contract_module_imports(module: str) -> None:
    importlib.import_module(module)


def _members(protocol: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(protocol) if not name.startswith("_")}


def test_message_bus_exposes_send_and_bind() -> None:
    from mlops_agents.agents.messaging.contract import MessageBus

    assert {"send", "bind"} <= _members(MessageBus)


def test_tasks_protocol_extends_task_workspaces() -> None:
    from mlops_agents.agents.tasks.contract import Tasks, TaskWorkspaces

    assert {"current", "files", "view"} <= _members(TaskWorkspaces)
    assert _members(TaskWorkspaces) <= _members(Tasks)
    assert {"start", "resume", "commit", "push", "local_merge", "end"} <= _members(Tasks)


def test_exceptions_carry_messages() -> None:
    from mlops_agents.agents.tasks.contract import ClaimConflict, CommitRefused

    conflict = ClaimConflict("coder already claims a.py in tsk_2.", agent="coder", task_id="tsk_2")
    assert str(conflict) == "coder already claims a.py in tsk_2."
    assert (conflict.agent, conflict.task_id) == ("coder", "tsk_2")
    assert str(CommitRefused("nothing to commit")) == "nothing to commit"
