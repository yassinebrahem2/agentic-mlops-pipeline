from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.read import ReadTool


async def test_reads_with_line_numbers(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "src/app.py"})
    assert not out.is_error
    assert out.content.splitlines()[2] == "     3\tdef train(model):"


async def test_offset_and_limit(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "src/app.py", "offset": 3, "limit": 2})
    assert out.content.splitlines()[:2] == [
        "     3\tdef train(model):",
        "     4\t    return model.fit()",
    ]
    assert "continues after line 4" in out.content


async def test_large_file_is_cut_with_a_note(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "big.txt"})
    lines = out.content.splitlines()
    assert lines[1999].endswith("line 2000")
    assert "Read again with offset=2001" in lines[-1]


async def test_binary_and_non_utf8(ctx: ToolContext) -> None:
    binary = await ReadTool().invoke(ctx, {"file_path": "data.bin"})
    assert "binary file" in binary.content
    latin = await ReadTool().invoke(ctx, {"file_path": "latin1.txt"})
    assert "caf� au lait" in latin.content


async def test_errors_are_results(ctx: ToolContext) -> None:
    assert (await ReadTool().invoke(ctx, {"file_path": "untracked.txt"})).is_error
    assert (await ReadTool().invoke(ctx, {"path": "README.md"})).is_error
