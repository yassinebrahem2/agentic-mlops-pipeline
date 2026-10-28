from __future__ import annotations

from pathlib import Path

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.containers import (
    build_run_args,
    container_name,
    mounts_fingerprint,
)
from mlops_agents.sandbox.projects import SandboxMount as Mount


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
    assert f"{APP_NAME}.mounts=4f53cda18c2baa0c" in [
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


def test_mounts_fingerprint() -> None:
    # Empty set has fixed value
    assert mounts_fingerprint([]) == "4f53cda18c2baa0c"

    m1 = Mount(host_path=Path("/host/data/a"), container_path="/data/alpha")
    m2 = Mount(host_path=Path("/host/data/b"), container_path="/data/beta")

    # Order-independent and stable
    fp1 = mounts_fingerprint([m1, m2])
    fp2 = mounts_fingerprint([m2, m1])
    assert fp1 == fp2
    assert len(fp1) == 16
    assert all(c in "0123456789abcdef" for c in fp1)

    # Different host path produces different fingerprint
    m1_diff_host = Mount(host_path=Path("/host/data/other"), container_path="/data/alpha")
    assert mounts_fingerprint([m1_diff_host, m2]) != fp1

    # Different container path (e.g. connection name) produces different fingerprint
    m1_diff_container = Mount(host_path=Path("/host/data/a"), container_path="/data/gamma")
    assert mounts_fingerprint([m1_diff_container, m2]) != fp1


def test_build_run_args_with_mounts(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_mounts",
        title="Test Task Mounts",
        branch="task-branch",
        worktree=worktree,
        status="active",
    )

    # Zero mounts
    args_zero = build_run_args("prj_1", task, "env:tag", mounts=[])
    assert f"{APP_NAME}.mounts=4f53cda18c2baa0c" in [
        args_zero[i + 1] for i, v in enumerate(args_zero) if v == "--label"
    ]
    assert "--mount" not in args_zero

    # One mount
    m_single = Mount(host_path=Path("/host/hotels"), container_path="/data/hotels")
    args_one = build_run_args("prj_1", task, "env:tag", mounts=[m_single])
    assert "--mount" in args_one
    vol_idx = args_one.index("-v")
    mount_idx = args_one.index("--mount")
    # Mount goes after worktree volume
    assert mount_idx > vol_idx
    expected_one_spec = f"type=bind,source={Path('/host/hotels')!s},target=/data/hotels,readonly"
    assert args_one[mount_idx + 1] == expected_one_spec

    # Two mounts: should be placed in container_path sorted order
    m_beta = Mount(host_path=Path("/host/beta_dir"), container_path="/data/beta")
    m_alpha = Mount(host_path=Path("/host/alpha_dir"), container_path="/data/alpha")
    # Pass them in reverse container_path order
    args_two = build_run_args("prj_1", task, "env:tag", mounts=[m_beta, m_alpha])

    # Find all --mount arguments
    mount_indices = [i for i, v in enumerate(args_two) if v == "--mount"]
    assert len(mount_indices) == 2
    assert mount_indices[0] > args_two.index("-v")
    spec_alpha = f"type=bind,source={Path('/host/alpha_dir')!s},target=/data/alpha,readonly"
    spec_beta = f"type=bind,source={Path('/host/beta_dir')!s},target=/data/beta,readonly"
    assert args_two[mount_indices[0] + 1] == spec_alpha
    assert args_two[mount_indices[1] + 1] == spec_beta

    # Label matches fingerprint
    expected_fp = mounts_fingerprint([m_alpha, m_beta])
    assert f"{APP_NAME}.mounts={expected_fp}" in [
        args_two[i + 1] for i, v in enumerate(args_two) if v == "--label"
    ]
