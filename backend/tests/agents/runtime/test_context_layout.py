from __future__ import annotations

from pathlib import Path

from mlops_agents.agents.runtime.context import top_level_layout


class FakeFiles:
    def __init__(self, tracked_paths: list[str]) -> None:
        self._paths = tracked_paths

    @property
    def root(self) -> Path:
        return Path("/repo")

    async def tracked(self) -> list[str]:
        return list(self._paths)

    async def under(self, prefix: str) -> list[str]:
        return [p for p in self._paths if p.startswith(prefix)]

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return user_path or ""

    def invalidate(self) -> None:
        return None


async def test_top_level_layout_grouping_and_sorting() -> None:
    files = FakeFiles(
        [
            "src/mlops_agents/main.py",
            "src/mlops_agents/utils.py",
            "README.md",
            "tests/test_store.py",
            "pyproject.toml",
            ".gitignore",
        ]
    )
    layout = await top_level_layout(files)
    expected = "\n".join([".gitignore", "README.md", "pyproject.toml", "src/", "tests/"])
    assert layout == expected


async def test_top_level_layout_caps_at_100() -> None:
    # 105 files in root
    paths = [f"file_{i:03d}.txt" for i in range(105)]
    files = FakeFiles(paths)
    layout = await top_level_layout(files)
    lines = layout.splitlines()
    assert len(lines) == 101
    assert lines[0] == "file_000.txt"
    assert lines[99] == "file_099.txt"
    assert lines[100] == "(and 5 more)"
