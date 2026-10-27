from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.config import APP_NAME
from mlops_agents.events.contract import EventRecord, EventType
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


class BlockingRunner(FakeDockerRunner):
    def __init__(
        self, started: asyncio.Event | None = None, cancelled: asyncio.Event | None = None
    ) -> None:
        super().__init__()
        self.started, self.cancelled = started, cancelled

    async def run(
        self, args: Sequence[str], *, timeout_s: float | None = None, secrets: Iterable[str] = ()
    ) -> DockerResult:
        if args and args[0] == "build":
            if self.started:
                self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if self.cancelled:
                    self.cancelled.set()
                raise
        return await super().run(args, timeout_s=timeout_s, secrets=secrets)


class BlockingRecordEventLog(FakeEventLog):
    def __init__(
        self,
        block_type: EventType | None = None,
        started: asyncio.Event | None = None,
        unblock: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self.block_type, self.started, self.unblock, self.fail_on_failed = (
            block_type,
            started,
            unblock,
            False,
        )

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        if self.fail_on_failed and type == "sandbox.build.failed":
            raise RuntimeError("event log store unreachable")
        if self.block_type and type == self.block_type:
            if self.started:
                self.started.set()
            if self.unblock:
                await self.unblock.wait()
        return await super().record(
            project_id, source=source, type=type, payload=payload, subject_id=subject_id
        )


async def test_cancelled_rebuild_after_started_sets_failed_and_propagates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    build_started, build_cancelled = asyncio.Event(), asyncio.Event()
    events = FakeEventLog()
    runner = BlockingRunner(started=build_started, cancelled=build_cancelled)
    projects = MemorySandboxProjects({"prj_c": repo})
    service = SandboxService(runner, events=events, projects=projects)
    task = asyncio.create_task(service.rebuild("prj_c"))
    await build_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert build_cancelled.is_set()
    status = await service.environment("prj_c")
    assert status.state == "failed"
    assert status.detail == "Image build failed: CancelledError"
    assert [r.type for r in events.records] == ["sandbox.build.started", "sandbox.build.failed"]
    assert events.records[0].payload == {"tag": status.tag, "detail": None}
    assert events.records[1].payload == {"tag": status.tag, "detail": status.detail}


async def test_cancelled_rebuild_resilient_to_event_recording_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    build_started = asyncio.Event()
    events = BlockingRecordEventLog()
    projects = MemorySandboxProjects({"prj_e": repo})
    service = SandboxService(
        BlockingRunner(started=build_started), events=events, projects=projects
    )
    task = asyncio.create_task(service.rebuild("prj_e"))
    await build_started.wait()
    events.fail_on_failed = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = await service.environment("prj_e")
    assert status.state == "failed"
    assert status.detail == "Image build failed: CancelledError"
    assert any(
        "Failed to record sandbox.build.failed event" in r.message and r.exc_info is not None
        for r in caplog.records
    )


async def test_cancelled_rebuild_during_finished_record_propagates_cancellation(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    record_started, record_unblock = asyncio.Event(), asyncio.Event()
    events = BlockingRecordEventLog(
        block_type="sandbox.build.finished", started=record_started, unblock=record_unblock
    )
    projects = MemorySandboxProjects({"prj_cf": repo})
    service = SandboxService(FakeDockerRunner(), events=events, projects=projects)
    task = asyncio.create_task(service.rebuild("prj_cf"))
    await record_started.wait()
    task.cancel()
    record_unblock.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = await service.environment("prj_cf")
    assert status.state == "ready"
    assert [r.type for r in events.records] == ["sandbox.build.started", "sandbox.build.finished"]


async def test_build_image_error_and_prior_oserror_have_same_detail_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlops_agents.sandbox import images
    from mlops_agents.sandbox import service as service_module

    def fail_snapshot(*args: object, **kwargs: object) -> None:
        raise OSError("failed to read requirements.txt")

    monkeypatch.setattr(service_module, "snapshot_image", fail_snapshot)
    projects_os = MemorySandboxProjects({"prj_os": tmp_path})
    service_os = SandboxService(FakeDockerRunner(), events=FakeEventLog(), projects=projects_os)
    status_os = await service_os.rebuild("prj_os")
    assert status_os.state == "failed"
    assert status_os.detail == "Image build failed: failed to read requirements.txt"

    monkeypatch.undo()

    async def fail_build(*args: object, **kwargs: object) -> object:
        raise RuntimeError("docker builder crashed")

    monkeypatch.setattr(images.ImageManager, "build_image", fail_build)
    projects_b = MemorySandboxProjects({"prj_b": tmp_path})
    service_b = SandboxService(FakeDockerRunner(), events=FakeEventLog(), projects=projects_b)
    status_build = await service_b.rebuild("prj_b")
    assert status_build.state == "failed"
    assert status_build.detail == "Image build failed: docker builder crashed"
