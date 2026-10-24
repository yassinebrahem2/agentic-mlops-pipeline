import os
from pathlib import Path

import pytest

from mlops_agents.errors import DomainError
from mlops_agents.workspace.models import Project
from mlops_agents.workspace.refs import (
    RepoRef,
    github_remote,
    https_url,
    parse_github_ref,
    path_key,
    repo_ref,
)


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


def _project(source: str, full_name: str | None) -> Project:
    return Project(
        id="prj_1",
        name="demo",
        source="github" if source == "github" else "local",
        local_path=None if source == "github" else "/work/demo",
        github_full_name=full_name,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-24T00:00:00Z",
        last_opened_at=None,
    )


def test_github_remote_of_a_github_project_ignores_the_origin() -> None:
    project = _project("github", "octo/demo")
    assert github_remote(project, None) == RepoRef(owner="octo", repo="demo")
    assert github_remote(project, "https://gitlab.com/a/b") == RepoRef(owner="octo", repo="demo")


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/octo/demo.git",
        "https://github.com/octo/demo",
        "git@github.com:octo/demo.git",
        "  git@github.com:octo/demo.git\n",
    ],
)
def test_github_remote_of_a_local_project_parses_a_github_origin(origin: str) -> None:
    assert github_remote(_project("local", None), origin) == RepoRef(owner="octo", repo="demo")


@pytest.mark.parametrize(
    "origin",
    [None, "", "https://gitlab.com/octo/demo.git", "git@gitlab.com:octo/demo.git", "foo/bar"],
)
def test_github_remote_is_none_without_a_github_origin(origin: str | None) -> None:
    # "foo/bar" is a relative path remote, not the owner/name shorthand parse_github_ref accepts.
    assert github_remote(_project("local", None), origin) is None


def test_https_url() -> None:
    assert https_url(RepoRef(owner="octo", repo="demo")) == "https://github.com/octo/demo.git"


def test_repo_ref() -> None:
    ref = repo_ref("octo/demo")
    assert ref == RepoRef(owner="octo", repo="demo")
    assert https_url(ref) == "https://github.com/octo/demo.git"
