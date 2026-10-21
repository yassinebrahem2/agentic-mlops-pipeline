import pytest

from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.glob import GlobTool, glob_to_regex


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/*.py", "src/nested/util.py", True),
        ("**/*.py", "app.py", True),
        ("*.py", "src/app.py", False),
        ("src/*.py", "src/app.py", True),
        ("src/*.py", "src/nested/util.py", False),
        ("**/*.{md,py}", "docs/guide.md", True),
        ("?.txt", "a.txt", True),
        ("[!a]*.txt", "big.txt", True),
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool) -> None:
    assert (glob_to_regex(pattern).fullmatch(path) is not None) == expected


async def test_glob_lists_tracked_matches(ctx: ToolContext) -> None:
    out = await GlobTool().invoke(ctx, {"pattern": "**/*.py"})
    assert sorted(out.content.splitlines()) == ["src/app.py", "src/nested/util.py"]


async def test_glob_relative_to_path(ctx: ToolContext) -> None:
    out = await GlobTool().invoke(ctx, {"pattern": "*.py", "path": "src"})
    assert out.content.splitlines() == ["src/app.py"]


async def test_glob_never_lists_secrets_or_untracked(ctx: ToolContext) -> None:
    out = await GlobTool().invoke(ctx, {"pattern": "**"})
    assert ".env" not in out.content.splitlines()
    assert "untracked.txt" not in out.content


async def test_no_match(ctx: ToolContext) -> None:
    assert (await GlobTool().invoke(ctx, {"pattern": "**/*.rs"})).content == "No files matched."


async def test_invalid_glob_is_an_error_result(ctx: ToolContext) -> None:
    out = await GlobTool().invoke(ctx, {"pattern": "[z-a].py"})
    assert out.is_error
    assert out.content.startswith("Invalid glob pattern")
