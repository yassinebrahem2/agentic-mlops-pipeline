import os
from pathlib import Path

import pytest

from mlops_agents.errors import DomainError
from mlops_agents.workspace.refs import parse_github_ref, path_key


@pytest.mark.parametrize(
    "text",
    [
        "octo/demo",
        "https://github.com/octo/demo",
        "https://github.com/octo/demo.git",
        "https://github.com/octo/demo/",
        "git@github.com:octo/demo.git",
        "  octo/demo  ",
    ],
)
def test_github_refs(text: str) -> None:
    assert parse_github_ref(text) == "octo/demo"


@pytest.mark.parametrize("text", ["", "demo", "https://gitlab.com/a/b", "a/b/c"])
def test_invalid_github_refs(text: str) -> None:
    with pytest.raises(DomainError):
        parse_github_ref(text)


def test_path_key_is_case_insensitive_on_windows(tmp_path: Path) -> None:
    folder = tmp_path / "Repo"
    folder.mkdir()
    variant = Path(str(folder).upper()) if os.name == "nt" else folder / "."
    assert path_key(variant) == path_key(folder)
