from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.sandbox.docker import CliDockerRunner
from mlops_agents.sandbox.service import SandboxService
from mlops_agents.storage.databases import Databases
from tests.sandbox.fakes import MemorySandboxProjects


@pytest.mark.docker
async def test_real_docker_mounts_lifecycle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = DataPaths(home)
    paths.ensure()

    databases = Databases(paths)
    await databases.app()
    project_id = "prj_mountdocker"
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
    task = TaskRef("tsk_mdocker_1", "Mount Docker Task", "branch", worktree, "active")

    try:
        # 1. /data/hotels/a.csv is visible from Sandbox.run
        res_read = await service.run(project_id, task, "cat /data/hotels/a.csv", timeout_s=30)
        assert res_read.exit_code == 0
        assert "1,alpha" in res_read.output

        # 2. Writes fail with a read-only error and host file is unchanged
        res_touch = await service.run(
            project_id, task, "touch /data/hotels/new_file.txt", timeout_s=30
        )
        assert res_touch.exit_code != 0
        assert "Read-only file system" in res_touch.output
        assert not (data_dir / "new_file.txt").exists()

        res_overwrite = await service.run(
            project_id, task, "echo 'corrupted' > /data/hotels/a.csv", timeout_s=30
        )
        assert res_overwrite.exit_code != 0
        assert "Read-only file system" in res_overwrite.output
        assert csv_file.read_text(encoding="utf-8") == orig_csv_content

        # 3. After deleting the connection, next run no longer sees /data/hotels
        await conns_service.delete(project_id, con.id)
        res_after = await service.run(project_id, task, "ls /data/hotels", timeout_s=30)
        assert res_after.exit_code != 0
        assert "No such file or directory" in res_after.output
    finally:
        await service.remove_task(project_id, task.id)
        await service.close()
        await databases.dispose()
