"""Tests for build_one_shot_run_args in sandbox/containers.py."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.containers import build_one_shot_run_args


@dataclass(frozen=True)
class DummyMount:
    host_path: Path
    container_path: str


def test_build_one_shot_run_args_defaults() -> None:
    project_id = "prj_123"
    image_tag = "mlops-agents-env:tag123"
    command = ["bash", "-lc", "python train.py"]

    args = build_one_shot_run_args(project_id, image_tag, command)

    # Core flags
    assert args[0] == "run"
    assert "--rm" in args
    assert any(args[i : i + 2] == ["--cpus", "2"] for i in range(len(args) - 1))
    assert any(args[i : i + 2] == ["--memory", "4g"] for i in range(len(args) - 1))
    assert any(args[i : i + 2] == ["--pids-limit", "512"] for i in range(len(args) - 1))
    assert any(args[i : i + 2] == ["--workdir", "/workspace"] for i in range(len(args) - 1))
    assert f"{APP_NAME}=true" in args
    assert f"{APP_NAME}.project={project_id}" in args

    # Network enabled by default: --network none should NOT be present
    assert "--network" not in args

    # No env file by default: --env-file should NOT be present
    assert "--env-file" not in args

    # Env vars
    assert "-e" in args
    assert "HOME=/tmp" in args
    assert "PYTHONPATH=/workspace/src:/workspace" in args

    # Image tag followed by command at the end
    assert args[-4:] == [image_tag, "bash", "-lc", "python train.py"]


def test_build_one_shot_run_args_network_false_and_no_env_file() -> None:
    args = build_one_shot_run_args(
        "prj_123",
        "img:tag",
        ["bash", "-lc", "echo 1"],
        network=False,
        env_file=None,
    )
    assert "--network" in args
    net_idx = args.index("--network")
    assert args[net_idx + 1] == "none"
    assert "--env-file" not in args


def test_build_one_shot_run_args_with_env_file_and_mounts(tmp_path: Path) -> None:
    env_path = tmp_path / "test_env.env"
    mount1 = DummyMount(host_path=Path("/host/data"), container_path="/data/conn1")
    mount2 = DummyMount(host_path=Path("/host/repo"), container_path="/workspace")

    args = build_one_shot_run_args(
        "prj_123",
        "img:tag",
        ["echo", "hello"],
        network=True,
        mounts=[mount2, mount1],  # passed unsorted
        env_file=env_path,
    )

    # env file present
    assert "--env-file" in args
    env_idx = args.index("--env-file")
    assert args[env_idx + 1] == str(env_path)

    # mounts sorted by container_path: /data/conn1 then /workspace
    mount_indices = [i for i, val in enumerate(args) if val == "--mount"]
    assert len(mount_indices) == 2
    m1_val = args[mount_indices[0] + 1]
    m2_val = args[mount_indices[1] + 1]
    assert "target=/data/conn1,readonly" in m1_val
    assert "target=/workspace,readonly" in m2_val
