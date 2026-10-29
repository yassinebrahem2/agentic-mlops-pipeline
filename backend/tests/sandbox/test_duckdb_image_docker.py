from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import DUCKDB_VERSION
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.paths import DataPaths
from mlops_agents.sandbox.docker import CliDockerRunner
from mlops_agents.sandbox.service import SandboxService
from mlops_agents.storage.databases import Databases
from tests.sandbox.fakes import MemorySandboxProjects


@pytest.mark.docker
async def test_real_docker_duckdb_requirements_image(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = DataPaths(home)
    paths.ensure()

    databases = Databases(paths)
    await databases.app()
    project_id = new_id("prj")
    await databases.project(project_id)

    events = SqliteEventLog(databases)
    registry = KindRegistry([LocalFolderKind(paths.root)])
    conns_service = ConnectionsService(databases, events, registry)

    runner = CliDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("six\n", encoding="utf-8")

    data_dir = tmp_path / "data_ext"
    data_dir.mkdir()
    orig_csv_content = "id,name\n1,alpha\n2,beta\n"
    csv_file = data_dir / "a.csv"
    csv_file.write_text(orig_csv_content, encoding="utf-8")

    con = await conns_service.create(
        project_id, LOCAL_FOLDER_KIND, "hotels", {"path": str(data_dir)}
    )

    projects = MemorySandboxProjects(
        roots={project_id: worktree},
        connections=conns_service,
    )
    service = SandboxService(runner=runner, projects=projects, events=events)
    task = TaskRef(new_id("tsk"), "DuckDB Requirements Task", "branch", worktree, "active")

    try:
        # 1. duckdb --version prints DUCKDB_VERSION
        res_ver = await service.run(project_id, task, "duckdb --version", timeout_s=300)
        assert res_ver.exit_code == 0, f"duckdb --version failed: {res_ver.output}"
        assert DUCKDB_VERSION in res_ver.output

        # 2. command -v duckdb is outside /workspace
        res_which = await service.run(project_id, task, "command -v duckdb", timeout_s=30)
        assert res_which.exit_code == 0, f"command -v duckdb failed: {res_which.output}"
        bin_path = res_which.output.strip()
        assert not bin_path.startswith("/workspace")
        assert bin_path == "/usr/local/bin/duckdb"

        # 3. duckdb -c "select count(*) from read_csv('/data/<name>/a.csv')" returns row count
        res_query = await service.run(
            project_id,
            task,
            "duckdb -c \"select count(*) from read_csv('/data/hotels/a.csv')\"",
            timeout_s=30,
        )
        assert res_query.exit_code == 0, f"duckdb query failed: {res_query.output}"
        assert "2" in res_query.output

        # 4. /proc/mounts lists, as binds of host paths, only /workspace and /data/* mounts
        res_mounts = await service.run(project_id, task, "cat /proc/mounts", timeout_s=30)
        assert res_mounts.exit_code == 0
        mount_lines = res_mounts.output.splitlines()
        docker_own = {
            "/etc/hosts",
            "/etc/hostname",
            "/etc/resolv.conf",
            "/usr/sbin/docker-init",
            "/",
        }
        pseudo_fstypes = {
            "proc",
            "sysfs",
            "tmpfs",
            "devpts",
            "mqueue",
            "cgroup",
            "cgroup2",
            "devtmpfs",
            "autofs",
            "overlay",
            "rootfs",
            "bpf",
            "tracefs",
            "securityfs",
            "pstore",
            "hugetlbfs",
            "ramfs",
            "binfmt_misc",
        }
        host_binds: list[str] = []
        for line in mount_lines:
            parts = line.split()
            if len(parts) >= 3:
                target = parts[1]
                fstype = parts[2]
                if target in docker_own or target.endswith("/docker-init"):
                    continue
                if fstype in pseudo_fstypes:
                    continue
                if target.startswith(("/proc", "/sys", "/dev")):
                    continue
                host_binds.append(target)
        assert sorted(host_binds) == ["/data/hotels", "/workspace"]

    finally:
        await conns_service.delete(project_id, con.id)
        await service.remove_task(project_id, task.id)
        await service.close()
        await databases.dispose()


@pytest.mark.docker
async def test_real_docker_duckdb_base_and_uv_images(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = DataPaths(home)
    paths.ensure()

    databases = Databases(paths)
    await databases.app()

    events = SqliteEventLog(databases)
    registry = KindRegistry([LocalFolderKind(paths.root)])
    conns_service = ConnectionsService(databases, events, registry)
    runner = CliDockerRunner()

    # 1. Base template (no dependency files)
    project_id_base = new_id("prj")
    await databases.project(project_id_base)
    worktree_base = tmp_path / "worktree_base"
    worktree_base.mkdir()

    projects_base = MemorySandboxProjects(
        roots={project_id_base: worktree_base},
        connections=conns_service,
    )
    service_base = SandboxService(runner=runner, projects=projects_base, events=events)
    task_base = TaskRef(new_id("tsk"), "DuckDB Base Task", "branch", worktree_base, "active")

    try:
        res_ver = await service_base.run(
            project_id_base, task_base, "duckdb --version", timeout_s=300
        )
        assert res_ver.exit_code == 0
        assert DUCKDB_VERSION in res_ver.output
    finally:
        await service_base.remove_task(project_id_base, task_base.id)
        await service_base.close()

    # 2. UV template (pyproject.toml and uv.lock)
    project_id_uv = new_id("prj")
    await databases.project(project_id_uv)
    worktree_uv = tmp_path / "worktree_uv"
    worktree_uv.mkdir()
    (worktree_uv / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = ["six"]\n',
        encoding="utf-8",
    )
    (worktree_uv / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "demo"\nversion = "0.1.0"\n'
        'source = { virtual = "." }\ndependencies = [{ name = "six" }]\n\n'
        '[package.metadata]\nrequires-dist = [{ name = "six" }]\n\n'
        '[[package]]\nname = "six"\nversion = "1.17.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.pythonhosted.org/packages/b7/ce/149a00dd41f10bc29e5921b496af8b574d8413afcd5e30dfa0ed46c2cc5e/six-1.17.0-py2.py3-none-any.whl", hash = "sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274", size = 11050 }]\n',
        encoding="utf-8",
    )

    projects_uv = MemorySandboxProjects(
        roots={project_id_uv: worktree_uv},
        connections=conns_service,
    )
    service_uv = SandboxService(runner=runner, projects=projects_uv, events=events)
    task_uv = TaskRef(new_id("tsk"), "DuckDB UV Task", "branch", worktree_uv, "active")

    try:
        res_ver = await service_uv.run(project_id_uv, task_uv, "duckdb --version", timeout_s=300)
        assert res_ver.exit_code == 0
        assert DUCKDB_VERSION in res_ver.output
    finally:
        await service_uv.remove_task(project_id_uv, task_uv.id)
        await service_uv.close()
        await databases.dispose()


@pytest.mark.docker
async def test_real_docker_duckdb_python_310_project_builds(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = DataPaths(home)
    paths.ensure()

    databases = Databases(paths)
    await databases.app()
    project_id = new_id("prj")
    await databases.project(project_id)

    events = SqliteEventLog(databases)
    registry = KindRegistry([LocalFolderKind(paths.root)])
    conns_service = ConnectionsService(databases, events, registry)
    runner = CliDockerRunner()

    worktree = tmp_path / "worktree_310"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("six\n", encoding="utf-8")
    (worktree / ".python-version").write_text("3.10\n", encoding="utf-8")

    projects = MemorySandboxProjects(
        roots={project_id: worktree},
        connections=conns_service,
    )
    service = SandboxService(runner=runner, projects=projects, events=events)
    task = TaskRef(new_id("tsk"), "Python 3.10 DuckDB Task", "branch", worktree, "active")

    try:
        res_py = await service.run(
            project_id,
            task,
            "python -c \"import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')\"",
            timeout_s=300,
        )
        assert res_py.exit_code == 0
        assert res_py.output.strip() == "3.10"

        res_ver = await service.run(project_id, task, "duckdb --version", timeout_s=30)
        assert res_ver.exit_code == 0
        assert DUCKDB_VERSION in res_ver.output
    finally:
        await service.remove_task(project_id, task.id)
        await service.close()
        await databases.dispose()
