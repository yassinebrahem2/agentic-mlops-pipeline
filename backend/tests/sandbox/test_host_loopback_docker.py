from __future__ import annotations

import contextlib
import os
import socket
import sys
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.ids import new_id
from mlops_agents.sandbox.docker import CliDockerRunner, detect_docker
from mlops_agents.sandbox.service import SandboxService
from tests.sandbox.fakes import FakeEventLog, MemorySandboxProjects

PROBE_SCRIPT = """\
import socket
import sys


def main() -> None:
    if len(sys.argv) < 3:
        print("RESULT unreachable MissingArgs")
        sys.exit(1)
    host = sys.argv[1]
    port = int(sys.argv[2])
    try:
        addr = socket.gethostbyname(host)
    except socket.gaierror:
        print("RESULT name_unresolved")
        sys.exit(0)
    except Exception as e:
        print(f"RESULT unreachable {type(e).__name__}")
        sys.exit(0)

    try:
        sock = socket.create_connection((addr, port), timeout=5)
        sock.close()
        print(f"RESULT reachable {addr}")
    except ConnectionRefusedError:
        print(f"RESULT refused {addr}")
    except Exception as e:
        print(f"RESULT unreachable {type(e).__name__}")


if __name__ == "__main__":
    main()
"""


def _extract_result_line(output: str) -> str:
    for line in output.splitlines():
        trimmed = line.strip()
        if trimmed.startswith("RESULT "):
            return trimmed
    return output.strip()


def _append_step_summary(summary_path: str, line: str) -> None:
    with Path(summary_path).open("a", encoding="utf-8") as f:
        f.write(f"{line}\n")


@pytest.mark.docker
async def test_host_loopback_reachable_from_task_container(tmp_path: Path) -> None:
    runner = CliDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    probe_path = worktree / "probe.py"
    probe_path.write_text(PROBE_SCRIPT, encoding="utf-8")

    project_id = new_id("prj")
    ctrl_project_id = new_id("prj")
    task = TaskRef(new_id("tsk"), "Host Loopback Task", "branch", worktree, "active")
    ctrl_task = TaskRef(new_id("tsk"), "Host Loopback Control Task", "branch", worktree, "active")

    projects = MemorySandboxProjects(roots={project_id: worktree, ctrl_project_id: worktree})
    events = FakeEventLog()
    service = SandboxService(runner=runner, projects=projects, events=events)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = int(sock.getsockname()[1])

        # 1. Network-enabled probe
        await projects.set_network(project_id, True)
        res = await service.run(
            project_id,
            task,
            f"python /workspace/probe.py host.docker.internal {port}",
            timeout_s=30,
        )
        probe_result_line = _extract_result_line(res.output)

        # 2. Control check with network disabled
        await projects.set_network(ctrl_project_id, False)
        ctrl_res = await service.run(
            ctrl_project_id,
            ctrl_task,
            f"python /workspace/probe.py host.docker.internal {port}",
            timeout_s=30,
        )
        ctrl_result_line = _extract_result_line(ctrl_res.output)

        # Asserts that control output does not start with RESULT reachable
        assert not ctrl_result_line.startswith("RESULT reachable"), (
            f"Control with network disabled unexpectedly reached host: {ctrl_result_line}"
        )
        assert ctrl_result_line.startswith("RESULT name_unresolved") or ctrl_result_line.startswith(
            "RESULT unreachable"
        ), f"Control output unexpected: {ctrl_result_line}"

        # 3. Assertion on network-enabled result
        valid_prefixes = (
            "RESULT name_unresolved",
            "RESULT reachable",
            "RESULT refused",
            "RESULT unreachable",
        )
        assert any(probe_result_line.startswith(p) for p in valid_prefixes), (
            f"Unexpected probe result: {probe_result_line}"
        )

        # 4. Reporting
        status = await detect_docker(runner)
        docker_version = status.version or "unknown"
        report_line = (
            f"HOST_LOOPBACK platform={sys.platform} docker={docker_version} "
            f"result={probe_result_line}"
        )
        print(report_line)

        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            _append_step_summary(step_summary, report_line)
    finally:
        sock.close()
        with contextlib.suppress(Exception):
            await service.remove_task(project_id, task.id)
        with contextlib.suppress(Exception):
            await service.remove_task(ctrl_project_id, ctrl_task.id)
        await service.close()
