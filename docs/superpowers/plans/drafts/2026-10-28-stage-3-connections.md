> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Connections Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the stage overview (`docs/superpowers/plans/2026-10-28-stage-3-overview.md` once it leaves `drafts/`) first.

**Goal:** the user's data folders become read-only connections (spec §4, §5):
- a per-project registry with the `local_folder` kind, its routes and a settings section;
- each connection mounted read-only into the project's task containers, the mounts part of the container's fingerprint;
- DuckDB (Python package and CLI) in every environment image, at `DUCKDB_VERSION`;
- the host-loopback measurement as a named Docker test.

**Branch:** `plan/stage-3-connections`. **Consumes:** I15 as the overview writes it (`ConnectionKind`, `Connections`, `Finding`, `Findings`, `TableRef`, `ColumnInfo`, `Mount`, `DuckDbSource`, `ConnectionView`, `ConnectionStatus`), and "Shared by contracts" (task 1): the prefix `con` in `ids.py`, the event types `connection.created`, `connection.updated` and `connection.deleted`, `DUCKDB_VERSION` in `config.py` with the `duckdb==2.0.0` dependency, and the schema's `connections` table (`id, name, kind, settings_json, credential_ref, status, status_detail, created, updated`; `name` is `UNIQUE` (project.db is per project, so there is no project column), which the service relies on; `credential_ref` stays null: this kind has no credential). **Produces:** `Connections` (I15) and its routes `GET/POST /api/projects/{id}/connections`, `POST .../connections/inspect`, `PATCH/DELETE .../connections/{con}`, `GET .../connections/{con}/tables`, and `SecretFilesWarning` from task 1 (early PR), and the folder browser working in server mode; the container fingerprint with mounts; DuckDB in the image; the Connections settings section.

**Read first:** spec §4, §5, §12 (settings, Connections), §14; stage 2 sandbox plan (`2026-10-24-stage-2-sandbox.md`); `sandbox/containers.py`, `sandbox/images.py`, `sandbox/service.py`, `sandbox/projects.py`.

**Docker in tests:** as in stage 2, real Docker tests carry `@pytest.mark.docker` and run on the Linux `docker` CI job (`-m docker`); everything else uses `FakeDockerRunner` (`tests/sandbox/fakes.py`). Windows team members run the Docker tests locally: `uv run pytest -m docker tests/sandbox/<file> -s`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The registry, `local_folder` and the routes | I15, contracts task 1 |
| 2 | Read-only mounts and the container fingerprint | task 1 |
| 3 | DuckDB in the environment image | task 2 |
| 4 | The host-loopback test | task 2 |
| 5 | The Connections settings section | task 1 |

Task 1 gets an early PR from `plan/stage-3-connections-task1`: data-summary consumes `Connections.duckdb_source`, `tables` and `data_version`. Tasks 2 to 5 go to the plan PR.

---

### Task 1: The registry, `local_folder` and the routes

**Files:** new `backend/src/mlops_agents/connections/registry.py` (`KindRegistry`), `connections/kinds/__init__.py` (`DATA_MOUNT_ROOT = "/data"`), `connections/kinds/local_folder.py` (`LocalFolderSettings`, `LocalFolderKind`), `connections/kinds/local_folder_scan.py` (listing, versions, the findings walk; keeps both files under 400 lines), `connections/service.py` (`ConnectionsService`), `backend/src/mlops_agents/api/routers/connections.py`, `api/services.py` (the `connections` field, built before the sandbox; the router is found by `include_routers`, no registration line), `workspace/service.py` (`browse` loses its server-mode refusal, see notes), tests (`tests/connections/test_local_folder.py`, `test_scan.py`, `test_service.py`, `tests/api/test_connections_routes.py`; the server-mode folder-browser tests in `tests/workspace/test_local.py` and `tests/api/test_projects.py` change), new `frontend/src/components/SecretFilesWarning.tsx` and `SecretFilesWarning.test.tsx`, regenerated `frontend/openapi.json` (`uv run python -m mlops_agents.api.openapi_export`; the only generated file committed, `frontend/src/api/schema.d.ts` is gitignored and made by `npm run gen:api`).

**Interfaces:**
- Consumes I15 as above, `DataPaths.root`, `Databases.project`, `EventLog.record`, `new_id("con")`, `now_iso`, `DomainError`, `workspace.fs.user_path`, `workspace.fs.browse_folder`, `agents.tools.files.is_secret` (the one secret denylist; never copy it).
- Produces `KindRegistry.get(name) -> ConnectionKind` and `.names()`; `LocalFolderKind(data_dir: Path)` implementing `ConnectionKind` (`name = "local_folder"`, `settings_model = LocalFolderSettings`, `needs_credential = False`; `findings`, `tables`, `data_version(settings, table: TableRef)`, `duckdb_source(settings, table: TableRef)`, `sandbox_mount(name, settings)` exactly as I15); `ConnectionsService(databases, events, registry)` implementing `Connections`; the routes with bodies `CreateConnection{kind: str, name: str, settings: dict[str, Any]}`, `InspectConnection{kind: str, settings: dict[str, Any]}` (returns `Findings`), `UpdateConnection{settings: dict[str, Any]}`; responses `ConnectionView`, `list[ConnectionView]`, `list[TableRef]`, 204. Errors are `DomainError`: `unknown_connection_kind` 400, `invalid_connection_name` 400, `connection_invalid` 400 (the kind's user-facing reason), `connection_name_taken` 409, `connection_not_found` 404, `table_not_found` 404 (data-summary's table-detail route answers the same code).

**Implementation notes:**
- **The folder check is one function, `LocalFolderKind._resolve(settings) -> Path`**, run by `validate`, and again by `findings`, `tables`, `data_version`, `duckdb_source` and `sandbox_mount` on every call, because a link can be retargeted after the connection was created. In order: `user_path(text)`; **refuse a path that is not absolute after `~` expansion** ("Give the full path of the folder."); then `Path.resolve(strict=True)` in a worker thread (resolves links, Windows junctions and 8.3 names); must be a directory the app can read (`os.scandir` it once; `PermissionError` and `OSError` become user-facing reasons, as `browse_folder` words them); **refuse a folder inside the data folder, equal to it, or containing it** (`data = paths.root.resolve()`; `folder.is_relative_to(data) or data.is_relative_to(folder)`; the containing case matters: a read-only mount of `AppData` would expose `keys.enc` and every `project.db`); refuse a path containing `,` or `"` (the mount syntax of task 2 cannot carry them) and a POSIX path containing `:`; **refuse a UNC or network path on Windows** (`\\server\share`, or a mapped drive whose `resolve()` turns into one): Docker Desktop cannot bind-mount it, and one unmountable connection would otherwise fail every container start of the project. Settings store the path as typed (stripped); only the resolved path is ever mounted or handed to DuckDB.
- **Server mode** (spec §4 rules): a local folder is a folder on the server, picked with the folder browser of the add-project wizard. `GET /api/folders` is refused in server mode today (`WorkspaceService.browse` calls `_require_local_mode`, because adding local projects is disabled there); this task removes that call from `browse` only (`add_local` and `relink` keep it). The route stays behind the app's login, and the add-project page keeps hiding its folder tab in server mode by itself. The checks (absolute, resolved, not in or containing the data folder, no UNC) are the same in both modes.
- **Tables** (scan with `os.scandir`, top level only, in a worker thread): supported extensions `.csv`, `.parquet`, `.json`, `.jsonl`, matched case-insensitively; each regular file is one table (`TableRef.files` = the one name); each direct subfolder is one table when its regular files (dotfiles ignored: the OS and Spark leave them) all share one supported extension. Names: the file stem, or the folder name; when two tables would share a name, the files among them use their full file name. Sorted by name. **Links never become table files**: `entry.is_symlink()` or `is_junction()` entries are unsupported ("a link"), so a link inside the folder cannot lead DuckDB (data-summary's `allowed_directories`) outside it. A subfolder with more than `MAX_TABLE_FILES = 10_000` files is unsupported ("too many files"). `TableRef.files` are POSIX paths relative to the folder.
- **`SecretFilesWarning`** (`{ findings: Findings }` from the generated types): renders nothing when `findings.secret_files` is empty; otherwise one `role="alert"` block with a fixed lead sentence ("The agents will be able to read these files; remove the connection to stop that."), then each finding as its `path` and its backend `reason` (the finding text is the backend's, never rebuilt here), and "and more" when `findings.truncated`. It lives in `components/` because the settings dialog (task 5) and the proposals plan's `connections` card both import it; neither writes its own warning.
- **Table lookup**: the service gets a table name from the caller, finds its `TableRef` in `tables()` and passes that to the kind; the name is never turned into a path, so `..`, a separator or an absolute path simply names no table (404 `table_not_found`).
- **`data_version`**: sha256 hex (first 16 characters) of `relpath\0size\0mtime_ns` lines over the table's sorted files, `os.stat(follow_symlinks=False)`; a vanished file raises `DomainError("connection_invalid", ...)`.
- **`duckdb_source`**: `DuckDbSource(sql_from, allowed_directories=[resolved folder])`. `sql_from` is `read_csv([...])`, `read_parquet([...])` or `read_json([...], format='auto')` (`'newline_delimited'` for `.jsonl`) over the table's absolute POSIX-style file paths (forward slashes also on Windows), each as a single-quoted literal with `'` doubled (the one escape DuckDB string literals have); multi-file tables add `union_by_name=true`. Never a glob, so the list is exactly what `tables()` returned.
- **`findings`**: `Findings.secret_files` lists every file in the folder whose name matches `is_secret` as `Finding(path=<relative POSIX path>, reason="matches a secret file name")` (a walk with `os.walk(followlinks=False)`; sorted, at most 100) because a read-only mount cannot hide them; `Findings.unsupported_files` lists the top-level files and subfolders that are not tables, each `Finding(path, reason)` ("not a supported format", "a link", "mixed formats", "too many files"). The walk stops after `MAX_SCAN_ENTRIES = 50_000` entries (one constant), and then `Findings.truncated` is true; hitting the cap of 100 listed files sets it too.
- **Service**: `inspect` runs the kind's `validate` and `findings` and writes nothing. `create` checks the kind, the name against `CONNECTION_NAME_PATTERN` (contracts task 1 defines it in `connections/contract.py` beside I15, so task 1 never needs the payload types of contracts task 2; if it lands elsewhere, import it from there, never copy it; `^[a-z0-9][a-z0-9-]{0,39}$`; safe in a container path, a URL and DuckDB; this plan defines no pattern of its own, and the frontend shows the server's message), parses `settings` with the kind's `settings_model` (a pydantic error becomes `connection_invalid`), calls `validate(settings, None)`, inserts (`id = new_id("con")`, `created` and `updated` from `now_iso`; the `UNIQUE` constraint's `IntegrityError` becomes `connection_name_taken`; no pre-check query), records `connection.created` (`subject_id` = the ID, payload `{name, kind}`), returns the `ConnectionView`. `update` keeps the ID and the name, replaces `settings` after the same parse and `validate`, sets `updated`, records `connection.updated`; unknown ID is 404. `list` builds each view live: `status` `ok`, or `unreachable` with the kind's reason as `status_detail`; `data_version` is the version string of the table whose newest file has the latest mtime (`None` when unreachable or without tables), from one `tables()` scan; the folder check, `findings` and the version run per view and never raise for one bad connection (a bad one gets empty findings, `truncated` false and no version). The row's `status` and `status_detail` columns are written when the live value changes; reads return the live value. `delete` removes the row and records `connection.deleted`. `tables`, `data_version`, `duckdb_source` load the row and delegate. `mounts(project_id)` returns the kind's `sandbox_mount(name, settings)` of every connection whose check passes, sorted by `container_path`; an unreachable connection is left out and is not an error (its status shows in the settings section; the Bash call goes on without that mount). The kind builds `container_path` as `f"{DATA_MOUNT_ROOT}/{name}"`.
- `list` runs the findings walk per connection on every GET, and the section refetches on each connection event; this is deliberate (a folder's contents change under the app, and the walk is capped), not an oversight. Cache it only if a measurement shows a problem.
- The routes only call the service; the project comes from `ProjectDep`. No credential is read or written: `needs_credential` is false for this kind.
- Errors from the OS go through the existing scrubbing like every domain error; a message names the folder the user typed, never the data folder's location.

**Tests (stop condition):**
- Kind, on `tmp_path` fixtures (a data folder `home` and a separate `data` folder):
  - a folder inside `home`, `home` itself and a folder containing `home` are refused, each with its message;
  - a relative path and a UNC path (`\\host\share\x`, a pure-path unit test that runs on every platform) are refused;
  - a link or Windows junction inside `data` pointing to a folder inside `home` is refused at creation, and a link retargeted into `home` after creation makes `tables` and `mounts` refuse it (the helper makes a symlink, falls back to `mklink /J` on Windows, and skips only if neither is possible);
  - a missing path, a file and an unreadable folder (`chmod 000` on POSIX, skipped on Windows) each give their reason; a path with `,` and a POSIX path with `:` are refused; server mode accepts the same folders as local mode (the same checks apply);
  - tables: CSV, Parquet, JSON and JSONL files, a subfolder of Parquet parts as one table, a mixed subfolder unsupported, `.xlsx` unsupported, upper-case extensions, a name collision (`a.csv` and `a.parquet` become `a.csv` and `a.parquet`), a link in the folder never a table, a subfolder over `MAX_TABLE_FILES` unsupported;
  - `data_version` is stable for unchanged files and changes when a file's size or mtime changes or a file is added to a folder table;
  - `duckdb_source`: the exact `sql_from` for a CSV, a Parquet folder and a JSONL file; a file name containing `'` is escaped; `allowed_directories` is the resolved folder;
  - `sandbox_mount`: host path is the resolved folder, container path `/data/<name>`;
  - `findings` lists `.env`, `prod.pem` and `sub/id_rsa` as secret files and nothing else, lists the unsupported entries with their reasons, and with more than `MAX_SCAN_ENTRIES` entries (or more than 100 secret files) it sets `truncated`; `ConnectionView.data_version` is stable for unchanged files and changes with a file's mtime.
- Service and routes (in-process app, `make_client`): create, list (with `ok`), tables, update (same ID and name, new folder; `connection.updated`), delete, and 404 after; `inspect` returns findings and writes no row and no event; a duplicate name is 409; an invalid name, an unknown kind and an invalid folder give their codes; the same create works in a server-mode app, and `GET /api/folders` answers there; deleting the folder on disk turns the view `unreachable` with the reason while the other connection stays `ok`; a table name `../x` is 404; the three events are in the project's events; one project never sees another's connections.
- `openapi_export --check` is clean and `npm run gen:api` runs; `SecretFilesWarning` Vitest (nothing for no secret files, paths and reasons shown, "and more" on `truncated`), `npm run lint`, `npm run format:check`, `npm run build`; full backend suite, ruff check and format, pyright; CI green.

**PR:** early PR into `main` from `plan/stage-3-connections-task1`, `Closes #<task 1 issue>`; data-summary needs `tables`, `data_version` and `duckdb_source`, and proposals task 4 needs `inspect` and `SecretFilesWarning`.

---

### Task 2: Read-only mounts and the container fingerprint

**Files:** `backend/src/mlops_agents/sandbox/containers.py`, `sandbox/projects.py`, `sandbox/service.py`, `api/services.py` (the `DatabaseSandboxProjects` line gains `connections`), tests (`tests/sandbox/test_container_args.py`, `test_containers.py`, `test_service_containers.py`, `tests/sandbox/fakes.py`'s `MemorySandboxProjects`, `tests/sandbox/test_mounts_docker.py`).

**Interfaces:**
- Consumes `Connections.mounts` and `Mount` (I15); `mounts()` already returns the full `container_path`, so `sandbox/` does not import from `connections/`.
- Produces `SandboxProjects.mounts(project_id) -> list[Mount]` (the existing seam grows one method; `DatabaseSandboxProjects(databases, workspace, connections)` delegates to `Connections.mounts`); `containers.mounts_fingerprint(mounts: Sequence[Mount]) -> str`; `build_run_args(..., mounts: Sequence[Mount] = ())`; `ContainerInfo.mounts: str | None`; `ContainerManager.start` and `ensure_container` take `mounts`.

**Implementation notes:**
- Each mount is `--mount type=bind,source=<host_path>,target=<container_path>,readonly` (not `-v`: `--mount` fails when the source is missing, where `-v` makes an empty directory on the host; the worktree's `-v` stays as is). Task 1 already refuses `,` and `"` in host paths and the name regex keeps the target safe. Mounts go after the worktree mount, in `container_path` order.
- The fingerprint is `sha256` (first 16 hex) of the canonical JSON of the sorted `[host_path, container_path]` pairs; the empty set has a fixed value. It is stored as the label `{APP_NAME}.mounts`, always, even when empty. `inspect` reads it into `ContainerInfo.mounts`; `ensure_container` keeps a running container only when image tag, network flag **and** mounts fingerprint all match; a container with no such label counts as a mismatch. Changing connections therefore recreates the project's containers at their next Bash call, with no eager removal and no extra locking: `SandboxService._ensure_container_locked` reads `self._projects.mounts(project_id)` under the task guard it already holds and passes it down.
- If `docker run` fails while mounts are present, the `SandboxUnavailable` message names the container paths ("... while mounting /data/hotel-data, /data/events: <docker's reason>"), so one bad mount is findable and the user can remove that connection. The start is not retried without the mounts: an agent must never silently run without the data it was told is there.
- A mount is read-only by construction (`readonly`), not by agent behavior; the plan adds no other write path.
- A project with no connections gets exactly today's arguments plus the empty-fingerprint label.

**Tests (stop condition):**
- With a fake runner: the exact `docker run` arguments with zero, one and two mounts (order, `readonly`, labels), unchanged otherwise (network flag and user as today); a `docker run` failure with mounts names the mount paths in the error; the fingerprint is stable, order-independent, and changes with a host path or a connection name; a running container with the same image and network but a different fingerprint, and one with no label, is removed and started again; an equal fingerprint keeps it (no `rm`, no `run`); creating, updating and deleting a connection through the real service changes what the next `ensure_container` does (`MemorySandboxProjects` fed from a `ConnectionsService` on a temp database).
- On the `docker` job (`test_mounts_docker.py`): a folder with `a.csv` is visible at `/data/<name>/a.csv` from `Sandbox.run`; a write (`touch /data/<name>/x`, `echo > /data/<name>/a.csv`) fails with a read-only error and the host file is unchanged; after deleting the connection the next `run` no longer sees `/data/<name>` (the container was recreated). The test passes on Windows when run locally.
- Full suite, checks; CI green.

---

### Task 3: DuckDB in the environment image

**Files:** new `sandbox/duckdb_cli.py` (per-architecture release asset names and sha256 digests), `sandbox/templates/_duckdb.fragment` (the shared Dockerfile lines), the five `sandbox/templates/*.Dockerfile` (one marker line `# @duckdb` before the final `WORKDIR`), `sandbox/images.py` (`load_template`), tests (`tests/sandbox/test_images.py`, `tests/test_build_wheel.py`, `tests/sandbox/test_duckdb_image_docker.py`).

**Interfaces:** consumes task 2 (the docker test reads a mounted connection) and `config.DUCKDB_VERSION` (contracts). Produces the tag change: every image tag changes once because the template text (hashed into the tag) gains the fragment; projects rebuild once at their next Bash call.

**Implementation notes:**
- `load_template` composes the template and the fragment (the marker line is replaced by the fragment's text) and then rewrites the `ARG` lines literally, line by line, as it does for `PYTHON_VERSION` today, now from one mapping: `PYTHON_VERSION`, `DUCKDB_VERSION` (from `config.DUCKDB_VERSION`), `DUCKDB_CLI_SHA256_AMD64`, `DUCKDB_CLI_SHA256_ARM64`. The fragment is part of the Dockerfile text, so the build context still holds only `Dockerfile` and the dependency files (the existing context test stays unchanged and passing).
- The fragment runs **after** the project's dependency install, so the app's version is the one left in the image even when a project pins another: `python -m ensurepip --upgrade && python -m pip install --no-cache-dir duckdb==${DUCKDB_VERSION}` (`ensurepip` because `uv sync` and conda environments may not keep `pip`), then the CLI: one `python -c` step (no curl or unzip in `python:slim`) that picks the asset by `platform.machine()` (`x86_64` to amd64, `aarch64` or `arm64` to arm64), downloads `https://github.com/duckdb/duckdb/releases/download/v${DUCKDB_VERSION}/duckdb_cli-linux-<arch>.zip`, **checks its sha256 against the build arg and fails the build on a mismatch**, extracts the one `duckdb` file to `/usr/local/bin/duckdb` and marks it executable. Both land outside `/workspace`. The implementer reads the real asset names and digests from the release page of the pinned version and records them in `sandbox/duckdb_cli.py`; never a guessed digest, never `latest`.
- The micromamba template installs into the conda env's Python when it has one, otherwise into the base image's; either way `python -c "import duckdb"` works in the sandbox shell, and the test below says which.
- A change of `DUCKDB_VERSION` or a digest changes every tag, which is intended.

**Tests (stop condition):**
- With a fake runner (every platform): for each of the five templates `load_template` contains the fragment, the pinned version and both digests and no `latest`; the version in the Dockerfile equals `DUCKDB_VERSION`; the tag changes when `DUCKDB_VERSION` or a digest changes and is stable otherwise; the fragment is read through `importlib.resources` like the templates, and `tests/test_build_wheel.py` gains one line asserting `_duckdb.fragment` is in the built wheel (the wheel packs every tracked file of `src/mlops_agents`, so this guards against a later filter); the build context lists only `Dockerfile` and the dependency files; `backend/pyproject.toml` pins `duckdb==` the `DUCKDB_VERSION` value.
- On the `docker` job: build the `requirements` template from a tiny `requirements.txt`: `duckdb --version` prints `DUCKDB_VERSION`, `python -c "import duckdb; print(duckdb.__version__)"` prints it, `command -v duckdb` is outside `/workspace`, and with a connection mounted (task 2) `duckdb -c "select count(*) from read_csv('/data/<name>/a.csv')"` returns the row count and `/proc/mounts` lists, as binds of host paths, only `/workspace` and the `/data/*` mounts (DuckDB is not the sandbox boundary; the container is). Build the `base` and `uv` templates too, so the shared fragment is proven on a venv without extra packages. The micromamba template is covered by the fake-runner tests only (its environment build is slow); say so in the PR.
- Full suite, checks; CI green.

---

### Task 4: The host-loopback test

**Files:** new `backend/tests/sandbox/test_host_loopback_docker.py`.

**Interfaces:** consumes `SandboxService`, `CliDockerRunner`, `MemorySandboxProjects`, `FakeEventLog`; produces nothing in code, only a measured answer that the demo's MLflow address follows from (spec §5).

**Implementation notes:**
- Named `test_host_loopback_reachable_from_task_container`, marked `docker`. It measures what the app really does, through `SandboxService.run` on a task whose project has network on (`MemorySandboxProjects.network`), without adding any flag the app does not add.
- On the host the test opens a listening socket bound to `127.0.0.1` on port 0 (listen only; the kernel completes the handshake) and writes a small probe script into the worktree. The probe, run as `python /workspace/probe.py host.docker.internal <port>` through `Sandbox.run`, prints exactly one line, `RESULT name_unresolved`, `RESULT reachable <addr>`, `RESULT refused <addr>` or `RESULT unreachable <exception name>`: `socket.gethostbyname`, then `socket.create_connection` with a 5 second timeout.
- A control proves the probe can fail: with network off, the same probe must print `RESULT name_unresolved` or `RESULT unreachable ...` (never `reachable`).
- The test asserts only that the probe printed one of the known results and that the control held. It does **not** assert that loopback is reachable: it is a measurement. It prints one line, `HOST_LOOPBACK platform=<sys.platform> docker=<server version> result=<the probe's result>`, and appends it to `$GITHUB_STEP_SUMMARY` when set, so the Linux CI job records its answer too.
- No `--add-host` is added by the test or by the app. On Linux Docker Engine the name does not resolve without one (expected: `name_unresolved`); on Docker Desktop it is built in. What the result decides: `reachable` on Docker Desktop for Windows means the demo's MLflow can stay bound to `127.0.0.1` and be addressed as `host.docker.internal`; anything else means it must listen on an address the container can reach, or a flag is added to the container (a decision for the lead, not part of this plan).

**Tests (stop condition):** run it on Docker Desktop for Windows (WSL2 backend) with the app's default settings and paste the printed `HOST_LOOPBACK` line, the Docker Desktop version and the backend (WSL2 or Hyper-V) into the plan PR under "how it was tested"; run it on the Linux `docker` job and note that line too. Full suite, checks; CI green with the test on the `docker` job.

---

### Task 5: The Connections settings section

**Files:** new `frontend/src/pages/project-settings/ConnectionsSection.tsx`, `ConnectionDialog.tsx` (add and edit), `ConnectionsSection.test.tsx`, `ConnectionDialog.test.tsx`; `ProjectSettingsPage.tsx` (one line: the section, after `SandboxSection`).

**Interfaces:** consumes the task 1 routes through the generated `schema.d.ts` types (`components["schemas"]["ConnectionView"]`, `Finding`, `Findings`, `TableRef`), `useAppInfo`, `FolderBrowser`, `useProjectEvents`, `api`/`unwrap`/`errorText`, the `testing.tsx` helpers (`stubRoutes`, `renderSection`). Consumes `SecretFilesWarning` from task 1. Produces the section.

**Implementation notes:**
- The section lists the project's connections: name, kind, status (`ok` or `unreachable` with its reason), the data version, the table count and, expanded, the tables with their formats, the unsupported files with their reasons, and, when `findings.secret_files` is not empty, `SecretFilesWarning` (task 1's component; the dialog's preview uses it too). Each row has Edit, and Remove with a confirm dialog. Query key `["projects", projectId, "connections"]`, refreshed on the events `connection.created`, `connection.updated` and `connection.deleted` (and on resync), as `SandboxSection` does.
- Add and Edit open `ConnectionDialog`: a name field (read-only when editing), the `FolderBrowser` already used by the add-project wizard (`onChoose` gives the path), and a **preview before saving**: choosing a folder calls `POST .../connections/inspect`; the findings (secret files as a warning, unsupported files) show before Create or Save is enabled, and an inspect refusal (data folder, relative path, UNC, not a folder) shows as the error. Create posts, Save patches; server errors show in the dialog as they come, including a taken name. The kind is `local_folder` only; there is no kind picker while one kind ships (the spec: the UI lists only the kinds that ship).
- In server mode (`useAppInfo().data.mode`) the dialog works the same, with one sentence above the folder browser: "This is a folder on the server."; the browser lists the server's disk.
- Per-table data versions are not shown here; the Data page shows them from the table-detail route.

**Tests (stop condition):** Vitest with stubbed routes: empty state; a list with an `ok` and an `unreachable` connection showing the reason; the secret-file warning appears only when `findings.secret_files` is non-empty; choosing a folder calls inspect and shows its findings or its refusal before Create is enabled; adding sends the chosen path and name and refreshes the list; editing sends a PATCH with the new path and keeps the name read-only; a 409 name clash shows its message in the dialog; remove asks to confirm, then calls DELETE and the row goes; a `connection.created` event refreshes the list; the data version and `truncated` render; server mode shows the sentence and still allows Add. `npm run lint`, `npm run format:check`, `npm test`, `npm run build`, then `npm run e2e` still passes (no e2e is added here: `flow.spec.ts` belongs to onboarding); CI green.

---

## Beyond the spec (the lead confirms or strikes)

Small rules the plan adds so the spec's guarantees ("no agent can read a host path outside a confirmed connection") hold on the real file systems:
- Links inside a connection's folder are never table files; a link is listed as unsupported.
- Folder paths containing `,` or `"` (and `:` on POSIX) and UNC or network paths on Windows are refused, because the bind mount is written with `--mount` and Docker Desktop cannot mount network paths.
- Dotfiles are ignored when deciding whether a subfolder is one table.
- Caps: 10,000 files per folder table, 50,000 entries for the findings walk, 100 listed secret files; a stopped walk sets `Findings.truncated`.
- The image tag of every project changes once (the template text gains DuckDB).

## Interface gaps

None open.
