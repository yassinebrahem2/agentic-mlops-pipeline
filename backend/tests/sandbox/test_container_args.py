from __future__ import annotations

from pathlib import Path

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.containers import (
    build_run_args,
    container_name,
)


def test_container_name() -> None:
    assert container_name("task_456") == f"{APP_NAME}-task_456"


def test_build_run_args_default_network_disabled(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_123",
        title="Test Task",
        branch="task-branch",
        worktree=worktree,
        status="active",
    )
    args = build_run_args(
        "prj_abc", task, "mlops-agents-env:prj_abc-1234567890ab", network=False, user=None
    )

    # Detached, init, and name
    assert args[0] == "run"
    assert "-d" in args
    assert "--init" in args
    name_idx = args.index("--name") + 1
    assert args[name_idx] == f"{APP_NAME}-task_123"

    # Labels
    assert f"{APP_NAME}=true" in [args[i + 1] for i, v in enumerate(args) if v == "--label"]
    assert f"{APP_NAME}.project=prj_abc" in [
        args[i + 1] for i, v in enumerate(args) if v == "--label"
    ]
    assert f"{APP_NAME}.task=task_123" in [
        args[i + 1] for i, v in enumerate(args) if v == "--label"
    ]
    assert f"{APP_NAME}.image_tag=mlops-agents-env:prj_abc-1234567890ab" in [
        args[i + 1] for i, v in enumerate(args) if v == "--label"
    ]
    assert f"{APP_NAME}.network=false" in [
        args[i + 1] for i, v in enumerate(args) if v == "--label"
    ]

    # Limits
    cpus_idx = args.index("--cpus") + 1
    assert args[cpus_idx] == "2"
    mem_idx = args.index("--memory") + 1
    assert args[mem_idx] == "4g"
    pids_idx = args.index("--pids-limit") + 1
    assert args[pids_idx] == "512"

    # Workdir and volume
    workdir_idx = args.index("--workdir") + 1
    assert args[workdir_idx] == "/workspace"
    vol_idx = args.index("-v") + 1
    assert args[vol_idx] == f"{worktree!s}:/workspace"

    # Network disabled
    assert "--network" in args
    net_idx = args.index("--network") + 1
    assert args[net_idx] == "none"

    # Environment
    env_vars = [args[i + 1] for i, v in enumerate(args) if v == "-e"]
    assert "HOME=/tmp" in env_vars
    assert "PYTHONPATH=/workspace/src:/workspace" in env_vars

    # Posix user omitted when user=None
    assert "-u" not in args

    # Image tag and entrypoint/command
    assert "mlops-agents-env:prj_abc-1234567890ab" in args
    img_idx = args.index("mlops-agents-env:prj_abc-1234567890ab")
    assert args[img_idx + 1 :] == ["sleep", "infinity"]


def test_build_run_args_network_enabled_and_posix(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_123",
        title="Test Task",
        branch="task-branch",
        worktree=worktree,
        status="active",
    )
    args = build_run_args(
        "prj_abc", task, "mlops-agents-env:prj_abc-1234567890ab", network=True, user="123:456"
    )

    assert "--init" in args
    # When network=True, --network none is omitted
    assert "--network" not in args
    assert f"{APP_NAME}.network=true" in [args[i + 1] for i, v in enumerate(args) if v == "--label"]

    # When user="123:456", -u <uid>:<gid> is included
    assert "-u" in args
    u_idx = args.index("-u") + 1
    assert args[u_idx] == "123:456"
