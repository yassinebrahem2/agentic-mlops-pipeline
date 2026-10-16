# Stage 1 — GitHub and Workspace Implementation Plan (DRAFT)

> **DRAFT: not ready to implement.** This plan sits in `drafts/` until both items below are done. Then it moves to `docs/superpowers/plans/` on its own `plan/stage-1-<topic>` branch and gets its issues.
> 1. **Advisor review.** Round 1 found blockers, and they are fixed. Rounds must continue until one finds no blocking issues.
> 2. **Check against the merged backend-core code.** backend-core changed while it was implemented (see PR #10, "done differently"). Every name in this plan's Consumes lines and test fixtures must match the code on `main`.


> **For agentic workers:** Implement this plan task by task, in order. Each task lists its exact files, the interfaces it consumes and produces, its tests (the task's stop condition) and the commands that prove it works. Claude Code users may use superpowers:executing-plans or superpowers:subagent-driven-development; nothing here requires them. Steps use checkbox (`- [ ]`) syntax. Team rules (claiming tasks, commits, PRs) are in `AGENTS.md`.

**Goal:** GitHub login (gh CLI or pasted token) and API calls, and the workspace: local and GitHub projects with clone, refresh, relink, removal and a server-side folder browser.

**Branch:** `plan/stage-1-github-workspace`. **Depends on:** backend-core (branch from main after it is merged).

**Read first:** `docs/superpowers/plans/2026-10-16-stage-1-overview.md` (global constraints, review focus, file structure), the spec `docs/superpowers/specs/2026-10-16-stage-1-foundation-design.md`, and `docs/roadmap.md`. The global constraints apply to every task below.

## Tasks

| Task | Title | Depends on |
|---|---|---|
| 1 | GitHub integration | none |
| 2 | Workspace | 1 |

---

### Task 1: GitHub integration

**Files:**
- Create: `backend/src/mlops_agents/integrations/github/api.py`, `auth.py`
- Test: `backend/tests/integrations/test_github_api.py`, `test_github_auth.py`, `test_live_clone.py`

**Interfaces:**
- Consumes: `Databases` (backend-core plan, Task 3), `KeyStore`, `error_text` (backend-core plan, Task 4), `DomainError`, `now_iso` (backend-core plan, Task 2), `GitRunner` (backend-core plan, Task 5).
- Produces:
  - `api.py`: `RepoSummary(full_name, private, default_branch, description)`, `async get_login(http, token) -> str`, `async list_repos(http, token, *, limit=300) -> list[RepoSummary]`, `async get_repo(http, token, full_name) -> RepoSummary`.
  - `auth.py`: `GitHubStatus(mode: Literal["none", "gh", "token"], login: str | None, gh_available: bool)`, `class GhLike(Protocol)` (`available` property, `async token() -> str`), `GhCli(path: str | None = None)`, `GitHubAuth(databases, keystore, http, gh: GhLike)` with `async status()`, `async enable_gh()`, `async set_token(value)`, `async disconnect()`, `async token() -> str | None`, `async require_token() -> str`.
  - `tests/conftest.py`: `make_repo(path: Path, files: dict[str, str], env: dict[str, str]) -> Path` (git init + commit; needs the `git_env` fixture's env).

- [ ] **Step 1: Write the failing tests**

`backend/tests/integrations/test_github_api.py`:

```python
import httpx
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import get_login, get_repo, list_repos

def _client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer good":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octo"})
        if request.url.path == "/user/repos":
            page = int(request.url.params["page"])
            items = [
                {"full_name": f"octo/r{page}-{i}", "private": True, "default_branch": "main", "description": None}
                for i in range(100 if page == 1 else 3)
            ]
            return httpx.Response(200, json=items)
        if request.url.path == "/repos/octo/demo":
            return httpx.Response(200, json={"full_name": "Octo/Demo", "private": False, "default_branch": "trunk", "description": "d"})
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))

async def test_login() -> None:
    assert await get_login(_client(), "good") == "octo"

async def test_rejected_token() -> None:
    with pytest.raises(DomainError) as err:
        await get_login(_client(), "bad")
    assert err.value.code == "github_token_rejected"

async def test_repos_are_paginated() -> None:
    repos = await list_repos(_client(), "good")
    assert len(repos) == 103

async def test_get_repo_and_missing_repo() -> None:
    repo = await get_repo(_client(), "good", "octo/demo")
    assert repo.full_name == "Octo/Demo"
    assert repo.default_branch == "trunk"
    with pytest.raises(DomainError) as err:
        await get_repo(_client(), "good", "octo/nope")
    assert err.value.code == "github_repo_not_found"
```

`backend/tests/integrations/test_github_auth.py`:

```python
import httpx
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GITHUB_KEYSTORE_REF, GitHubAuth
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore

class FakeGh:
    def __init__(self, value: str | None) -> None:
        self.value = value

    @property
    def available(self) -> bool:
        return True

    async def token(self) -> str:
        if self.value is None:
            raise DomainError("gh_not_logged_in", "not logged in", status=409)
        return self.value

def _http() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") in ("Bearer gho_a", "Bearer gho_b", "Bearer ghp_pasted"):
            return httpx.Response(200, json={"login": "octo"})
        return httpx.Response(401, json={"message": "Bad credentials"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))

async def test_gh_mode_reads_the_token_every_time(databases: Databases, keystore: MemoryKeyStore) -> None:
    gh = FakeGh("gho_a")
    auth = GitHubAuth(databases, keystore, _http(), gh)
    status = await auth.enable_gh()
    assert (status.mode, status.login) == ("gh", "octo")
    gh.value = "gho_b"  # the user logged in again with gh
    assert await auth.token() == "gho_b"
    assert keystore.data == {}

async def test_token_mode_stores_the_token(databases: Databases, keystore: MemoryKeyStore) -> None:
    auth = GitHubAuth(databases, keystore, _http(), FakeGh(None))
    status = await auth.set_token(" ghp_pasted ")
    assert status.mode == "token"
    assert keystore.data == {GITHUB_KEYSTORE_REF: "ghp_pasted"}
    assert await auth.token() == "ghp_pasted"
    await auth.disconnect()
    assert (await auth.status()).mode == "none"
    assert keystore.data == {}

async def test_rejected_token_is_not_stored(databases: Databases, keystore: MemoryKeyStore) -> None:
    auth = GitHubAuth(databases, keystore, _http(), FakeGh(None))
    with pytest.raises(DomainError):
        await auth.set_token("ghp_wrong")
    assert keystore.data == {}

async def test_require_token_when_not_connected(databases: Databases, keystore: MemoryKeyStore) -> None:
    auth = GitHubAuth(databases, keystore, _http(), FakeGh(None))
    with pytest.raises(DomainError) as err:
        await auth.require_token()
    assert err.value.code == "github_not_connected"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/integrations -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mlops_agents.integrations.github.api'`.

- [ ] **Step 3: Implement**

`backend/src/mlops_agents/integrations/github/api.py`:

```python
from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from mlops_agents.credentials.scrub import error_text
from mlops_agents.errors import DomainError

GITHUB_API = "https://api.github.com"
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

class RepoSummary(BaseModel):
    full_name: str
    private: bool
    default_branch: str
    description: str | None

async def _get(http: httpx.AsyncClient, token: str, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        response = await http.get(GITHUB_API + path, headers=headers, params=params, timeout=30)
    except httpx.HTTPError as exc:
        raise DomainError("github_unreachable", f"Could not reach GitHub: {error_text(exc)}", status=502) from None
    if response.status_code == 401:
        raise DomainError(
            "github_token_rejected",
            "GitHub rejected the token. Log in again with `gh auth login` or paste a new token in Integrations.",
            status=422,
        )
    return response

def _repo(item: dict[str, Any]) -> RepoSummary:
    return RepoSummary(
        full_name=str(item["full_name"]),
        private=bool(item.get("private")),
        default_branch=str(item.get("default_branch") or "main"),
        description=item.get("description") if isinstance(item.get("description"), str) else None,
    )

async def get_login(http: httpx.AsyncClient, token: str) -> str:
    response = await _get(http, token, "/user")
    if response.status_code != 200:
        raise DomainError("github_error", f"GitHub answered with HTTP {response.status_code}.", status=502)
    return str(response.json()["login"])

async def list_repos(http: httpx.AsyncClient, token: str, *, limit: int = 300) -> list[RepoSummary]:
    repos: list[RepoSummary] = []
    page = 1
    while len(repos) < limit:
        response = await _get(http, token, "/user/repos", {"per_page": 100, "page": page, "sort": "updated"})
        if response.status_code != 200:
            raise DomainError("github_error", f"GitHub answered with HTTP {response.status_code}.", status=502)
        items: list[dict[str, Any]] = response.json()
        repos.extend(_repo(item) for item in items)
        if len(items) < 100:
            break
        page += 1
    return repos[:limit]

async def get_repo(http: httpx.AsyncClient, token: str, full_name: str) -> RepoSummary:
    response = await _get(http, token, f"/repos/{full_name}")
    if response.status_code == 404:
        raise DomainError(
            "github_repo_not_found",
            f"{full_name} does not exist, or this GitHub account cannot see it.",
            status=404,
        )
    if response.status_code != 200:
        raise DomainError("github_error", f"GitHub answered with HTTP {response.status_code}.", status=502)
    return _repo(response.json())
```

`backend/src/mlops_agents/integrations/github/auth.py`:

```python
from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import now_iso
from mlops_agents.credentials import KeyStore
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import get_login
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import integrations

GITHUB = "github"
GITHUB_KEYSTORE_REF = "github/token"

class GitHubStatus(BaseModel):
    mode: Literal["none", "gh", "token"]
    login: str | None
    gh_available: bool

class GhLike(Protocol):
    @property
    def available(self) -> bool: ...

    async def token(self) -> str: ...

class GhCli:
    """The GitHub CLI. Its token is read on every use, never copied, so a new `gh auth login`
    is picked up automatically."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path if path is not None else shutil.which("gh")

    @property
    def available(self) -> bool:
        return self._path is not None

    async def token(self) -> str:
        if self._path is None:
            raise DomainError(
                "gh_not_installed",
                "The GitHub CLI (gh) is not installed. Install it, or paste a token instead.",
                status=409,
            )
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [self._path, "auth", "token", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise DomainError("gh_timeout", "The GitHub CLI did not answer within 30 seconds.", status=504) from None
        token = completed.stdout.strip()
        if completed.returncode != 0 or not token:
            raise DomainError(
                "gh_not_logged_in",
                "The GitHub CLI is not logged in. Run `gh auth login`, then try again.",
                status=409,
            )
        return token

class GitHubAuth:
    def __init__(self, databases: Databases, keystore: KeyStore, http: httpx.AsyncClient, gh: GhLike) -> None:
        self._dbs = databases
        self._keys = keystore
        self._http = http
        self._gh = gh

    async def _mode(self) -> tuple[str, str | None]:
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            row = (await conn.execute(select(integrations).where(integrations.c.name == GITHUB))).mappings().first()
        return (row["mode"], row["login"]) if row else ("none", None)

    async def _save(self, mode: str, login: str) -> None:
        engine = await self._dbs.app()
        values = {"name": GITHUB, "mode": mode, "login": login, "verified_at": now_iso()}
        statement = sqlite_insert(integrations).values(values)
        async with engine.begin() as conn:
            await conn.execute(
                statement.on_conflict_do_update(index_elements=["name"], set_={k: v for k, v in values.items() if k != "name"})
            )

    async def status(self) -> GitHubStatus:
        mode, login = await self._mode()
        return GitHubStatus(mode=mode, login=login, gh_available=self._gh.available)  # pyright: ignore[reportArgumentType] -- mode comes from our own table

    async def enable_gh(self) -> GitHubStatus:
        login = await get_login(self._http, await self._gh.token())
        await self._save("gh", login)
        await asyncio.to_thread(self._keys.delete, GITHUB_KEYSTORE_REF)
        return await self.status()

    async def set_token(self, value: str) -> GitHubStatus:
        value = value.strip()
        if not value:
            raise DomainError("token_empty", "Paste a token first.", status=422)
        login = await get_login(self._http, value)
        await asyncio.to_thread(self._keys.set, GITHUB_KEYSTORE_REF, value)
        await self._save("token", login)
        return await self.status()

    async def disconnect(self) -> None:
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(delete(integrations).where(integrations.c.name == GITHUB))
        await asyncio.to_thread(self._keys.delete, GITHUB_KEYSTORE_REF)

    async def token(self) -> str | None:
        mode, _ = await self._mode()
        if mode == "gh":
            return await self._gh.token()
        if mode == "token":
            return await asyncio.to_thread(self._keys.get, GITHUB_KEYSTORE_REF)
        return None

    async def require_token(self) -> str:
        token = await self.token()
        if token is None:
            raise DomainError("github_not_connected", "Connect GitHub in Integrations first.", status=409)
        return token
```

- [ ] **Step 4: Run tests and checks**

Run: `cd backend && uv run ruff format && uv run pytest tests/integrations -v && uv run ruff check && uv run pyright`
Expected: all pass on Windows and Linux (CI runs Linux; run it locally on Windows). 

- [ ] **Step 5: Live clone check** (marker `live`) — add `backend/tests/integrations/test_live_clone.py`:

```python
import os
from pathlib import Path

import pytest

from mlops_agents.integrations.github.auth import GhCli
from mlops_agents.integrations.github.git import GitRunner

pytestmark = pytest.mark.live

async def test_clone_private_repo_with_gh_token(tmp_path: Path) -> None:
    repo = os.environ.get("MLOPS_AGENTS_LIVE_GITHUB_REPO")
    if not repo:
        pytest.skip("set MLOPS_AGENTS_LIVE_GITHUB_REPO=owner/name (a private repo you can read)")
    gh = GhCli()
    runner = GitRunner(tmp_path / "bin", gh.token)
    await runner.run(["clone", "--depth", "1", f"https://github.com/{repo}.git", str(tmp_path / "c")], authenticated=True)
    assert (tmp_path / "c" / ".git").is_dir()
    assert "x-access-token" not in (tmp_path / "c" / ".git" / "config").read_text()
```

Run on Windows and Linux: `MLOPS_AGENTS_LIVE_GITHUB_REPO=yassinebrahem2/agentic-mlops-pipeline uv run pytest -m live tests/integrations/test_live_clone.py -v`
Expected: PASS, with no Git Credential Manager window appearing.

- [ ] **Step 6: Commit**

```bash
git add backend/src/mlops_agents/integrations backend/tests/integrations
git commit -m "feat(github): add GitHub API client and auth"
```

---

### Task 2: Workspace

**Files:**
- Create: `backend/src/mlops_agents/workspace/__init__.py` (empty), `models.py`, `refs.py`, `service.py`
- Test: `backend/tests/workspace/__init__.py` (empty), `test_refs.py`, `test_local.py`, `test_github.py`

**Interfaces:**
- Consumes: `Settings`, `RunMode`, `DataPaths`, `new_id`, `now_iso`, `DomainError` (backend-core plan, Task 2), `Databases` (backend-core plan, Task 3), `GitRunner`, `GitError` (backend-core plan, Task 5), `GitHubAuth`, `get_repo` (Task 1).
- Produces:
  - `models.py`: `ProjectSource = Literal["local", "github"]`, `ProjectStatus = Literal["ok", "missing", "empty", "clone_failed"]`, `Project(id, name, source, local_path, github_full_name, default_branch, status, status_detail, created_at, last_opened_at)`, `FolderEntry(name, path, is_git_repo)`, `FolderListing(path, parent, entries, roots)`.
  - `refs.py`: `path_key(path: Path) -> str`, `parse_github_ref(text: str) -> str`.
  - `WorkspaceService(settings, paths, databases, git: GitRunner, github: GitHubAuth, http: httpx.AsyncClient, *, remote_url: Callable[[str], str] = github_https_url)` with async `list_projects() -> list[Project]`, `get(project_id) -> Project`, `add_local(path_text, *, init_git=False) -> Project`, `add_github(ref_text) -> Project`, `open(project_id) -> Project`, `refresh(project_id) -> Project`, `relink(project_id, path_text) -> Project`, `remove(project_id) -> None`, `browse(path_text: str | None) -> FolderListing`, and sync `repo_root(project) -> Path`.
  - Messages shown to users: `EMPTY_REPO_DETAIL = "The repository has no committed files yet. Commit your files first."` (the agent-runtime plan reuses it).

- [ ] **Step 1: Write the failing tests**

`backend/tests/workspace/test_refs.py`:

```python
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
```

`backend/tests/workspace/test_local.py`:

```python
import shutil
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.integrations.test_github_auth import FakeGh


def _settings(paths: DataPaths, mode: RunMode = RunMode.LOCAL) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=mode)


def _service(paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, mode: RunMode = RunMode.LOCAL) -> WorkspaceService:
    http = httpx.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    return WorkspaceService(_settings(paths, mode), paths, databases, GitRunner(paths.bin, github.token), github, http)


async def test_add_local_repo(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    repo = make_repo(tmp_path / "demo", {"README.md": "# Demo"}, git_env)
    svc = _service(paths, databases, keystore)
    project = await svc.add_local(str(repo))
    assert (project.name, project.source, project.status, project.default_branch) == ("demo", "local", "ok", "main")
    assert project.id.startswith("prj_")
    assert svc.repo_root(project) == repo.resolve()
    with pytest.raises(DomainError) as err:
        await svc.add_local(str(repo / "."))
    assert err.value.code == "project_exists"


async def test_not_a_repo_offers_git_init(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    svc = _service(paths, databases, keystore)
    with pytest.raises(DomainError) as err:
        await svc.add_local(str(folder))
    assert err.value.code == "not_a_git_repo"
    assert err.value.detail == {"can_init": True}
    project = await svc.add_local(str(folder), init_git=True)
    assert project.status == "empty"
    assert project.status_detail == "The repository has no committed files yet. Commit your files first."


async def test_subfolder_of_a_repo_is_refused(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    repo = make_repo(tmp_path / "demo", {"src/a.py": "x = 1"}, git_env)
    with pytest.raises(DomainError) as err:
        await _service(paths, databases, keystore).add_local(str(repo / "src"))
    assert err.value.code == "not_repo_root"


async def test_server_mode_refuses_local_folders(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore) -> None:
    svc = _service(paths, databases, keystore, RunMode.SERVER)
    with pytest.raises(DomainError) as err:
        await svc.add_local(str(tmp_path))
    assert err.value.code == "local_projects_disabled"
    with pytest.raises(DomainError):
        await svc.browse(None)


async def test_missing_folder_and_relink(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    repo = make_repo(tmp_path / "demo", {"a.py": "x = 1"}, git_env)
    svc = _service(paths, databases, keystore)
    project = await svc.add_local(str(repo))
    moved = tmp_path / "moved"
    shutil.move(repo, moved)
    opened = await svc.open(project.id)
    assert opened.status == "missing"
    relinked = await svc.relink(project.id, str(moved))
    assert relinked.status == "ok"
    assert relinked.local_path == str(moved.resolve())


async def test_remove_keeps_the_users_repo(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    repo = make_repo(tmp_path / "demo", {"a.py": "x = 1"}, git_env)
    svc = _service(paths, databases, keystore)
    project = await svc.add_local(str(repo))
    await databases.project(project.id)  # the project database exists and is open
    await svc.remove(project.id)
    assert not paths.project_dir(project.id).exists()
    assert (repo / "a.py").is_file()
    assert await svc.list_projects() == []


async def test_browse_lists_folders(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    make_repo(tmp_path / "b-repo", {"a.py": "x"}, git_env)
    (tmp_path / "A-folder").mkdir()
    (tmp_path / "file.txt").write_text("x")
    listing = await _service(paths, databases, keystore).browse(str(tmp_path))
    names = [(e.name, e.is_git_repo) for e in listing.entries]
    assert ("A-folder", False) in names
    assert ("b-repo", True) in names
    assert all(e.name != "file.txt" for e in listing.entries)
    assert listing.parent == str(tmp_path.parent)
```

`backend/tests/workspace/test_github.py`:

```python
import subprocess
from pathlib import Path

import httpx
import pytest

from mlops_agents.config import RunMode, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.integrations.test_github_auth import FakeGh


def _bare_remote(tmp_path: Path, env: dict[str, str]) -> tuple[Path, Path]:
    work = make_repo(tmp_path / "work", {"README.md": "first"}, env)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], env=env, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=work, env=env, check=True, capture_output=True)
    return work, bare


async def _service(paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, bare: Path) -> WorkspaceService:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octo"})
        if request.url.path.lower() == "/repos/octo/demo":
            return httpx.Response(200, json={"full_name": "Octo/Demo", "private": True, "default_branch": "main", "description": None})
        return httpx.Response(404, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    github = GitHubAuth(databases, keystore, http, FakeGh("gho_test"))
    await github.enable_gh()
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return WorkspaceService(settings, paths, databases, GitRunner(paths.bin, github.token), github, http, remote_url=lambda _: bare.as_uri())


async def test_add_github_clones_into_app_data(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    _, bare = _bare_remote(tmp_path, git_env)
    svc = await _service(paths, databases, keystore, bare)
    project = await svc.add_github("https://github.com/octo/demo")
    assert (project.name, project.github_full_name, project.status) == ("Demo", "Octo/Demo", "ok")
    assert (svc.repo_root(project) / "README.md").read_text() == "first"
    with pytest.raises(DomainError) as err:
        await svc.add_github("OCTO/DEMO")
    assert err.value.code == "project_exists"


async def test_refresh_survives_force_push(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    work, bare = _bare_remote(tmp_path, git_env)
    svc = await _service(paths, databases, keystore, bare)
    project = await svc.add_github("octo/demo")
    (work / "README.md").write_text("rewritten")
    subprocess.run(["git", "commit", "-am", "rewrite", "--amend"], cwd=work, env=git_env, check=True, capture_output=True)
    subprocess.run(["git", "push", "--force", "origin", "main"], cwd=work, env=git_env, check=True, capture_output=True)
    refreshed = await svc.refresh(project.id)
    assert refreshed.status == "ok"
    assert (svc.repo_root(project) / "README.md").read_text() == "rewritten"


async def test_failed_refresh_keeps_the_last_copy(tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, git_env: dict[str, str]) -> None:
    _, bare = _bare_remote(tmp_path, git_env)
    svc = await _service(paths, databases, keystore, bare)
    project = await svc.add_github("octo/demo")
    bare.rename(tmp_path / "gone.git")  # the remote is gone, like a revoked token or a deleted repo
    refreshed = await svc.refresh(project.id)
    assert refreshed.status == "ok"
    assert refreshed.status_detail is not None
    assert refreshed.status_detail.startswith("Refresh failed")
    assert (svc.repo_root(project) / "README.md").read_text() == "first"
```

`tests/integrations/__init__.py` exists (backend-core plan, Task 5), so `FakeGh` is importable from `tests.integrations.test_github_auth`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/workspace -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mlops_agents.workspace'`.

- [ ] **Step 3: Implement**

`backend/src/mlops_agents/workspace/models.py`:

```python
from typing import Literal

from pydantic import BaseModel

ProjectSource = Literal["local", "github"]
ProjectStatus = Literal["ok", "missing", "empty", "clone_failed"]


class Project(BaseModel):
    id: str
    name: str
    source: ProjectSource
    local_path: str | None
    github_full_name: str | None
    default_branch: str | None
    status: ProjectStatus
    status_detail: str | None
    created_at: str
    last_opened_at: str | None


class FolderEntry(BaseModel):
    name: str
    path: str
    is_git_repo: bool


class FolderListing(BaseModel):
    path: str
    parent: str | None
    entries: list[FolderEntry]
    roots: list[str]
```

`backend/src/mlops_agents/workspace/refs.py`:

```python
import os
import re
from pathlib import Path

from mlops_agents.errors import DomainError

_OWNER = r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))"
_REPO = r"(?P<repo>[A-Za-z0-9._-]+?)"
_PATTERNS = (
    re.compile(rf"^https?://github\.com/{_OWNER}/{_REPO}(?:\.git)?/?$"),
    re.compile(rf"^git@github\.com:{_OWNER}/{_REPO}(?:\.git)?$"),
    re.compile(rf"^{_OWNER}/(?P<repo>[A-Za-z0-9._-]+)$"),
)


def path_key(path: Path) -> str:
    """A comparable identity for a folder: resolved, and case-folded on Windows."""
    resolved = str(path.resolve(strict=True))
    return resolved.casefold() if os.name == "nt" else resolved


def parse_github_ref(text: str) -> str:
    value = text.strip()
    for pattern in _PATTERNS:
        match = pattern.match(value)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    raise DomainError(
        "invalid_github_repo", "Enter a GitHub repository as owner/name or its URL.", status=422
    )
```

`backend/src/mlops_agents/workspace/service.py`:

```python
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import string
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import RowMapping, delete, insert, select, update

from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode, Settings
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.api import get_repo
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitError, GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.workspace.models import FolderEntry, FolderListing, Project, ProjectStatus
from mlops_agents.workspace.refs import parse_github_ref, path_key

EMPTY_REPO_DETAIL = "The repository has no committed files yet. Commit your files first."
MISSING_DETAIL = "The folder no longer exists. Point the project to its new location."
NOT_A_REPO_DETAIL = "The folder is no longer a git repository."


def github_https_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def _rmtree(path: Path) -> None:
    def make_writable_and_retry(func: Callable[..., Any], target: str, exc: BaseException) -> None:
        del exc
        Path(target).chmod(stat.S_IWRITE)  # git marks pack files read-only; Windows refuses to delete them
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable_and_retry)


class WorkspaceService:
    def __init__(
        self,
        settings: Settings,
        paths: DataPaths,
        databases: Databases,
        git: GitRunner,
        github: GitHubAuth,
        http: httpx.AsyncClient,
        *,
        remote_url: Callable[[str], str] = github_https_url,
    ) -> None:
        self._settings = settings
        self._paths = paths
        self._dbs = databases
        self._git = git
        self._github = github
        self._http = http
        self._remote_url = remote_url

    # Queries

    async def list_projects(self) -> list[Project]:
        engine = await self._dbs.app()
        query = select(projects).order_by(
            projects.c.last_opened_at.is_(None), projects.c.last_opened_at.desc(), projects.c.created_at.desc()
        )
        async with engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_project(row) for row in rows]

    async def get(self, project_id: str) -> Project:
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            row = (await conn.execute(select(projects).where(projects.c.id == project_id))).mappings().first()
        if row is None:
            raise DomainError("project_not_found", "This project does not exist.", status=404)
        return _project(row)

    def repo_root(self, project: Project) -> Path:
        if project.source == "local" and project.local_path is not None:
            return Path(project.local_path)
        return self._paths.repo_dir(project.id)

    # Local projects

    def _require_local_mode(self) -> None:
        if self._settings.mode is RunMode.SERVER:
            raise DomainError(
                "local_projects_disabled",
                "This server only works with GitHub repositories.",
                status=403,
            )

    async def _repo_folder(self, path_text: str, *, init_git: bool) -> tuple[Path, str]:
        folder = Path(path_text.strip()).expanduser()
        if not folder.is_dir():
            raise DomainError("folder_not_found", f"{folder} is not a folder on this machine.", status=422)
        key = await asyncio.to_thread(path_key, folder)
        top = await self._git.run(["rev-parse", "--show-toplevel"], cwd=folder, check=False)
        if top.code != 0:
            if not init_git:
                raise DomainError(
                    "not_a_git_repo",
                    f"{folder} is not a git repository. The app can run git init in it for you.",
                    status=422,
                    detail={"can_init": True},
                )
            await self._git.run(["init"], cwd=folder)
        elif await asyncio.to_thread(path_key, Path(top.stdout.strip())) != key:
            root = top.stdout.strip()
            raise DomainError(
                "not_repo_root",
                f"This folder is inside the repository {root}. Add that folder instead.",
                status=422,
                detail={"repo_root": root},
            )
        return folder.resolve(), key

    async def add_local(self, path_text: str, *, init_git: bool = False) -> Project:
        self._require_local_mode()
        folder, key = await self._repo_folder(path_text, init_git=init_git)
        await self._ensure_unique(local_path_key=key)
        status, detail = await self._status(folder)
        project_id = new_id("prj")
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                insert(projects).values(
                    id=project_id,
                    name=folder.name,
                    source="local",
                    local_path=str(folder),
                    local_path_key=key,
                    default_branch=await self._current_branch(folder),
                    status=status,
                    status_detail=detail,
                    created_at=now_iso(),
                )
            )
        return await self.get(project_id)

    async def relink(self, project_id: str, path_text: str) -> Project:
        self._require_local_mode()
        project = await self.get(project_id)
        if project.source != "local":
            raise DomainError("not_local", "Only local projects can be pointed to a new folder.", status=409)
        folder, key = await self._repo_folder(path_text, init_git=False)
        await self._ensure_unique(local_path_key=key, except_id=project_id)
        status, detail = await self._status(folder)
        await self._update(
            project_id,
            local_path=str(folder),
            local_path_key=key,
            default_branch=await self._current_branch(folder),
            status=status,
            status_detail=detail,
        )
        return await self.get(project_id)

    async def browse(self, path_text: str | None) -> FolderListing:
        self._require_local_mode()
        folder = Path(path_text).expanduser() if path_text else Path.home()
        if not folder.is_dir():
            raise DomainError("folder_not_found", f"{folder} is not a folder on this machine.", status=404)

        def scan() -> list[FolderEntry]:
            found: list[FolderEntry] = []
            for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
                try:
                    if child.is_dir():
                        found.append(FolderEntry(name=child.name, path=str(child), is_git_repo=(child / ".git").exists()))
                except OSError:
                    continue
            return found

        try:
            entries = await asyncio.to_thread(scan)
        except PermissionError:
            raise DomainError("folder_unreadable", f"The app is not allowed to read {folder}.", status=403) from None
        if os.name == "nt":
            roots = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
        else:
            roots = ["/"]
        parent = str(folder.parent) if folder.parent != folder else None
        return FolderListing(path=str(folder), parent=parent, entries=entries, roots=roots)

    # GitHub projects

    async def _clone(self, project_id: str, full_name: str) -> tuple[ProjectStatus, str | None]:
        target = self._paths.repo_dir(project_id)
        await asyncio.to_thread(_rmtree, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        result = await self._git.run(
            ["clone", "--origin", "origin", self._remote_url(full_name), str(target)],
            check=False,
            authenticated=True,
        )
        if result.code != 0:
            reason = result.stderr.strip() or "no output"
            return "clone_failed", f"Cloning failed: {reason}. Use Refresh to try again."
        return await self._status(target)

    async def add_github(self, ref_text: str) -> Project:
        full_name = parse_github_ref(ref_text)
        token = await self._github.require_token()
        repo = await get_repo(self._http, token, full_name)
        key = repo.full_name.casefold()
        await self._ensure_unique(github_key=key)
        project_id = new_id("prj")
        status, detail = await self._clone(project_id, repo.full_name)
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                insert(projects).values(
                    id=project_id,
                    name=repo.full_name.split("/", 1)[1],
                    source="github",
                    github_full_name=repo.full_name,
                    github_key=key,
                    default_branch=repo.default_branch,
                    status=status,
                    status_detail=detail,
                    created_at=now_iso(),
                )
            )
        return await self.get(project_id)

    async def _sync_clone(self, project: Project) -> None:
        """Fetch and reset the managed clone to the remote's default branch. Never edited by the
        app, so a hard reset is safe; it also survives force-pushes and renamed branches."""
        if project.github_full_name is None:
            return
        target = self._paths.repo_dir(project.id)
        if not (target / ".git").is_dir():
            status, detail = await self._clone(project.id, project.github_full_name)
            await self._update(project.id, status=status, status_detail=detail)
            return
        fetch = await self._git.run(["fetch", "--prune", "origin"], cwd=target, check=False, authenticated=True)
        if fetch.code != 0:
            status, _ = await self._status(target)
            reason = fetch.stderr.strip() or "no output"
            await self._update(
                project.id,
                status=status,
                status_detail=f"Refresh failed: {reason}. The last downloaded copy is still used.",
            )
            return
        try:
            await self._git.run(["remote", "set-head", "origin", "--auto"], cwd=target, check=False, authenticated=True)
            head = await self._git.run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=target, check=False)
            branch = head.stdout.strip().removeprefix("origin/") or project.default_branch or "main"
            await self._git.run(["checkout", "--force", "-B", branch, f"origin/{branch}"], cwd=target)
            await self._git.run(["reset", "--hard", f"origin/{branch}"], cwd=target)
            await self._git.run(["clean", "-ffdx"], cwd=target)
        except GitError as err:
            status, detail = await self._status(target)
            await self._update(project.id, status=status, status_detail=detail or f"Refresh failed: {err}")
            return
        status, detail = await self._status(target)
        await self._update(project.id, default_branch=branch, status=status, status_detail=detail)

    # Both kinds

    async def refresh(self, project_id: str) -> Project:
        project = await self.get(project_id)
        if project.source == "github":
            await self._sync_clone(project)
        else:
            root = self.repo_root(project)
            status, detail = await self._status(root)
            branch = await self._current_branch(root) if status != "missing" else project.default_branch
            await self._update(project_id, status=status, status_detail=detail, default_branch=branch)
        return await self.get(project_id)

    async def open(self, project_id: str) -> Project:
        await self.get(project_id)
        await self._update(project_id, last_opened_at=now_iso())
        return await self.refresh(project_id)

    async def remove(self, project_id: str) -> None:
        await self.get(project_id)
        await self._dbs.close_project(project_id)
        await asyncio.to_thread(_rmtree, self._paths.project_dir(project_id))
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(delete(projects).where(projects.c.id == project_id))

    # Helpers

    async def _status(self, root: Path) -> tuple[ProjectStatus, str | None]:
        if not root.is_dir():
            return "missing", MISSING_DETAIL
        top = await self._git.run(["rev-parse", "--show-toplevel"], cwd=root, check=False)
        if top.code != 0:
            return "missing", NOT_A_REPO_DETAIL
        tracked = await self._git.run(["ls-files", "-z"], cwd=root)
        if not tracked.stdout:
            return "empty", EMPTY_REPO_DETAIL
        return "ok", None

    async def _current_branch(self, root: Path) -> str | None:
        result = await self._git.run(["symbolic-ref", "--short", "HEAD"], cwd=root, check=False)
        return result.stdout.strip() or None

    async def _ensure_unique(
        self, *, local_path_key: str | None = None, github_key: str | None = None, except_id: str | None = None
    ) -> None:
        condition = (
            projects.c.local_path_key == local_path_key
            if local_path_key is not None
            else projects.c.github_key == github_key
        )
        if except_id is not None:
            condition = condition & (projects.c.id != except_id)
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            row = (await conn.execute(select(projects.c.id, projects.c.name).where(condition))).mappings().first()
        if row is not None:
            raise DomainError(
                "project_exists",
                f"This repository is already the project {row['name']}.",
                status=409,
                detail={"project_id": row["id"]},
            )

    async def _update(self, project_id: str, **values: object) -> None:
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(update(projects).where(projects.c.id == project_id).values(**values))


def _project(row: RowMapping) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        source=row["source"],
        local_path=row["local_path"],
        github_full_name=row["github_full_name"],
        default_branch=row["default_branch"],
        status=row["status"],
        status_detail=row["status_detail"],
        created_at=row["created_at"],
        last_opened_at=row["last_opened_at"],
    )
```

- [ ] **Step 4: Run tests and checks**

Run: `cd backend && uv run ruff format && uv run pytest tests/workspace -v && uv run ruff check && uv run pyright`
Expected: all pass on Windows and Linux.

- [ ] **Step 5: Commit**

```bash
git add backend/src/mlops_agents/workspace backend/tests/workspace
git commit -m "feat(workspace): add local and GitHub projects with refresh, relink and removal"
```
