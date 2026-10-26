from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.docker import DockerResult
from mlops_agents.sandbox.service import SandboxService
from tests.sandbox.fakes import FakeDockerRunner, FakeEventLog, MemorySandboxProjects


async def test_context_failure_sets_failed_status_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlops_agents.sandbox import images

    def fail_context(*args: object, **kwargs: object) -> Path:
        raise PermissionError("context directory denied")

    events = FakeEventLog()
    service = SandboxService(
        FakeDockerRunner(), events=events, projects=MemorySandboxProjects({"prj_123": tmp_path})
    )
    monkeypatch.setattr(images, "create_build_context", fail_context)
    status = await service.rebuild("prj_123")
    assert status.state == "failed"
    assert "context directory denied" in (status.detail or "")
    assert (await service.environment("prj_123")) == status
    assert [record.type for record in events.records] == [
        "sandbox.build.started",
        "sandbox.build.failed",
    ]
    assert events.records[-1].payload == {"tag": status.tag, "detail": status.detail}


async def test_build_error_after_started_ends_in_failed_with_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlops_agents.sandbox import images
    from mlops_agents.sandbox.contract import SandboxUnavailable

    async def fail_build(*args: object, **kwargs: object) -> object:
        raise SandboxUnavailable("Cannot connect to the Docker daemon")

    events = FakeEventLog()
    service = SandboxService(
        FakeDockerRunner(), events=events, projects=MemorySandboxProjects({"prj_123": tmp_path})
    )
    monkeypatch.setattr(images.ImageManager, "build_image", fail_build)
    status = await service.rebuild("prj_123")
    assert status.state == "failed"
    assert "Cannot connect to the Docker daemon" in (status.detail or "")
    current = await service.environment("prj_123")
    assert current.state == "failed"
    assert [record.type for record in events.records] == [
        "sandbox.build.started",
        "sandbox.build.failed",
    ]
    assert events.records[-1].payload == {"tag": status.tag, "detail": status.detail}


async def test_dependency_read_failure_records_failed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlops_agents.sandbox import service as service_module

    def fail_snapshot(*args: object, **kwargs: object) -> None:
        raise PermissionError("dependency denied")

    events = FakeEventLog()
    service = SandboxService(
        FakeDockerRunner(), events=events, projects=MemorySandboxProjects({"prj_123": tmp_path})
    )
    monkeypatch.setattr(service_module, "snapshot_image", fail_snapshot)
    status = await service.rebuild("prj_123")
    assert status.state == "failed"
    assert "dependency denied" in (status.detail or "")
    assert events.records[-1].type == "sandbox.build.failed"
    assert events.records[-1].payload == {"tag": None, "detail": status.detail}


async def test_failed_environment_is_invalidated_by_dependency_change(tmp_path: Path) -> None:
    dependency = tmp_path / "requirements.txt"
    dependency.write_text("broken\n", encoding="utf-8")
    runner = FakeDockerRunner()
    runner.fail_build = True
    service = SandboxService(
        runner, events=FakeEventLog(), projects=MemorySandboxProjects({"prj_123": tmp_path})
    )
    failed = await service.rebuild("prj_123")
    assert failed.state == "failed"
    dependency.write_text("six\n", encoding="utf-8")
    current = await service.environment("prj_123")
    assert current.state == "none"
    assert current.tag != failed.tag


async def test_environment_does_not_hide_unexpected_failures(tmp_path: Path) -> None:
    class BrokenDocker(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            raise RuntimeError("unexpected runner defect")

    service = SandboxService(
        BrokenDocker(), events=FakeEventLog(), projects=MemorySandboxProjects({"prj_123": tmp_path})
    )
    with pytest.raises(RuntimeError, match="unexpected runner defect"):
        await service.environment("prj_123")


async def test_service_rebuild_success_and_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    runner = FakeDockerRunner()
    events = FakeEventLog()
    service = SandboxService(
        runner, events=events, projects=MemorySandboxProjects({"prj_123": repo})
    )

    status = await service.rebuild("prj_123")
    assert status.state == "ready"
    assert status.tag is not None
    assert status.tag.startswith(f"{APP_NAME}-env:prj_123-")
    assert status.detail is None

    assert len(events.records) == 2
    started = events.records[0]
    finished = events.records[1]

    assert started.type == "sandbox.build.started"
    assert started.source == "system"
    assert started.payload == {"tag": status.tag, "detail": None}

    assert finished.type == "sandbox.build.finished"
    assert finished.source == "system"
    assert finished.payload == {"tag": status.tag, "detail": None}


async def test_service_rebuild_failure_and_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    runner = FakeDockerRunner()
    runner.fail_build = True
    events = FakeEventLog()
    service = SandboxService(
        runner, events=events, projects=MemorySandboxProjects({"prj_123": repo})
    )

    status = await service.rebuild("prj_123")
    assert status.state == "failed"
    assert status.tag is not None
    assert "Syntax error" in (status.detail or "")

    assert len(events.records) == 2
    failed = events.records[1]
    assert failed.type == "sandbox.build.failed"
    assert failed.source == "system"
    assert failed.payload == {"tag": status.tag, "detail": status.detail}


async def test_service_rebuild_serializes_concurrent_calls(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    runner = FakeDockerRunner()
    runner.default_build_delay = 0.05
    events = FakeEventLog()
    service = SandboxService(
        runner, events=events, projects=MemorySandboxProjects({"prj_123": repo})
    )

    task1 = asyncio.create_task(service.rebuild("prj_123"))
    task2 = asyncio.create_task(service.rebuild("prj_123"))

    res1, res2 = await asyncio.gather(task1, task2)
    assert res1.state == "ready"
    assert res2.state == "ready"
    assert res1.tag == res2.tag
    assert runner.build_calls == 1


async def test_environment_returns_failed_on_symlinked_dependency(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    req = worktree / "requirements.txt"
    external = tmp_path / "secret.env"
    external.write_text("SECRET=123", encoding="utf-8")
    try:
        req.symlink_to(external)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symlinks; exercised on Linux CI")
        raise

    service = SandboxService(
        FakeDockerRunner(),
        projects=MemorySandboxProjects({"prj_symlink": worktree}),
        events=FakeEventLog(),
    )
    status = await service.environment("prj_symlink")
    assert status.state == "failed"
    assert "symlink" in (status.detail or "").lower()


async def test_environment_returns_failed_on_value_error_python_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlops_agents.sandbox import service as service_module

    def fail_snapshot(*args: object, **kwargs: object) -> None:
        raise ValueError("invalid python version format")

    monkeypatch.setattr(service_module, "snapshot_image", fail_snapshot)
    service = SandboxService(
        FakeDockerRunner(),
        projects=MemorySandboxProjects({"prj_val": tmp_path}),
        events=FakeEventLog(),
    )
    status = await service.environment("prj_val")
    assert status.state == "failed"
    assert "invalid python version format" in (status.detail or "")
