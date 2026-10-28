> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Data Summary Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the [stage 3 overview](2026-10-28-stage-3-overview.md) first.

**Goal:** agents get context from one hook registry, and the `data_summary` hook gives them a capped, cached, locked-down summary of every table of the project's connections (spec §6):
- one injection point in the agent runtime calls `HookRegistry.run`; the stage 2 status summary becomes its first hook, `status_board`;
- DuckDB runs in the app process, locked to the connection's folder;
- results (never data copies) are cached per data version, 500 MB per project, least recently used evicted;
- `GET /api/projects/{id}/connections/{con}/tables/{table}` returns the same cached summary for the Data page.

**Branch:** `plan/stage-3-data-summary`. **Consumes:** I15 (`Connections`, `TableRef`, `TableDetail`, `DuckDbSource`, `ConnectionView`) from connections task 1; I18's `HookContext` and `ContextHook` (`agents/hooks/contract.py`), `DUCKDB_VERSION` (`config.py`) and the `duckdb==2.0.0` dependency, all from contracts task 1. **Produces:** `HookRegistry` (I18) from task 1 (early PR); the `data_summary` hook; `GET /api/projects/{id}/connections/{con}/tables/{table}` returning `TableDetail`; the registry is called by every plan that registers a hook (tracking, knowledge), in `build_services` before `AgentCatalog.load`.

**Read first:** spec §6, §4 (the `duckdb_source` method) and §14; overview I15 and I18; `agents/catalog.py`, `agents/runtime/agent.py`, `agents/runtime/context.py`, `agents/runtime/executor.py`, `events/status.py`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The hook registry, the injection point and `status_board` | contracts task 1 |
| 2 | The locked DuckDB session and the table profile | contracts task 1, connections task 1 |
| 3 | The result cache | task 2, connections task 1 |
| 4 | The `data_summary` hook | tasks 1 to 3, connections task 1 |
| 5 | The table-detail route | task 4 |

Task 1 starts after contracts task 1 only; tasks 2 to 5 also wait for connections task 1 (the overview's Starts). Task 1 gets an early PR, from `plan/stage-3-data-summary-task1`: the tracking and knowledge plans register their hooks on the registry.

---

### Task 1: The hook registry, the injection point and `status_board`

**Files:**
- new `backend/src/mlops_agents/agents/hooks/registry.py` (`HookRegistry`), `agents/hooks/names.py` (`STATUS_BOARD_HOOK = "status_board"`, the one source of the name; the other hooks' names live with their plans), `agents/hooks/status_board.py` (`StatusBoardHook`); `agents/hooks/__init__.py` if contracts did not create it;
- `agents/catalog.py` (remove `KNOWN_HOOKS`; `hook_names`; `AgentDefinition.hook_names`), `agents/runtime/agent.py` (`AgentBuilder`, `Agent`), `agents/runtime/context.py` (`build_messages` only), `agents/runtime/agent.py` also holds `resume_command`, `agents/runtime/executor.py` (the first `build(...)` and the resume `build(...)` calls gain `session_id` and `turn_id`; the resume's `Command(resume=True)` becomes `agent.resume_command(...)`; the two `build_messages` calls rename `status_summary=` to `hook_context=`), `agents/runtime/sessions.py` (the `AgentBuilder(...)` construction: `hooks=` and `store=` replace `status_board=`; `SessionManager(status_board=)` becomes `hooks=`), `api/services.py` (its lines only: create the registry, register `StatusBoardHook`, pass `hooks.names()` to `AgentCatalog.load`, bind the board). `executor.py` and `sessions.py` are listed for data-summary in the overview's Files shared (rate-limits adds its own lines to both; touch only the lines named here);
- tests: new `tests/agents/hooks/test_registry.py`, `tests/agents/hooks/test_status_board.py`; adapted `tests/agents/test_catalog.py`, `tests/agents/runtime/test_agent.py`, `tests/agents/runtime/test_context.py`, `tests/events/test_status.py`.

**Interfaces:**
- Consumes `HookContext(project_id, agent, trigger_text)` and `ContextHook` (`name`, `async render(ctx) -> str`) from contracts; `StatusBoard.summary` (`events/contract.py`).
- Produces `HookRegistry` exactly as I18: `register(hook)`, `names() -> frozenset[str]`, `async run(names: Sequence[str], ctx: HookContext) -> str`.
- Produces `AgentDefinition.hook_names: tuple[str, ...]` (property): `context_hooks`, plus `STATUS_BOARD_HOOK` when `status_board` is true and the name is not already listed. Onboarding deletes the field and simplifies the property to `context_hooks`.
- Changes `AgentCatalog.__init__` and `AgentCatalog.load` to take `hook_names: frozenset[str] = frozenset()` (keyword, after `tool_names`): `context_hooks` entries are validated against it ("unknown context hooks", as today). The `status_board` field is not validated against it: it is its own declaration until onboarding removes it. The default keeps the 63 existing call sites in 24 test files unchanged; production passes `hooks.names()`.
- Changes `Agent.status_summary` to `Agent.hook_context: str | None`, `build_messages(..., status_summary=)` to `hook_context=`, and `AgentBuilder(status_board=)` and `SessionManager(status_board=)` to `hooks: HookRegistry | None = None`, and `AgentBuilder` gains `store: SessionStore` (to read the trigger). `AgentBuilder.build(project_id, agent, *, session_id: str | None = None, turn_id: str | None = None)`: hooks run only when both IDs are given (both executor paths pass them); without them, none run.

**Implementation notes:**
- `run` renders the named hooks concurrently (`asyncio.gather`) and joins the non-empty results with a blank line, in the order of `names`. It raises nothing a hook raises: an unknown name gives the line `[<name>: unknown context hook]`; an `Exception` from a hook is logged with its traceback (`logger.exception`) and gives `[<name>: unavailable (<credentials.scrub.error_text(exc)>)]`. `CancelledError` is not an `Exception` and propagates, so Stop still works. Registering a name twice raises `ValueError`.
- Hooks cap their own text (the registry does not cut: one cap per hook, each in its owner's constants). `StatusBoardHook.render` returns `"Status board:\n" + summary` (the label `build_messages` adds today moves into the hook), or `""` when the summary is empty. It is bound after the status board exists (`bind(board)`, the way `ListAgentsTool.bind` is): the catalog needs the registry's names before `SqliteStatusBoard` (which needs the catalog) can be built, so `services.py` registers an unbound `StatusBoardHook`, loads the catalog with `hooks.names()`, then binds. Unbound, `render` returns its own line `[status_board: unavailable (not bound)]` (a hook never raises, as I18 says; the registry's catch is only a backstop).
- `AgentBuilder.build` runs `self._hooks.run(definition.hook_names, HookContext(...))` when both IDs are given, `hooks` is set and `hook_names` is not empty. The builder reads the trigger itself with `store.turn_trigger_id(project_id, turn_id)` then `store.message(project_id, session_id, id)`: the joined `TextPart` texts, `""` when there is none (a wake-up, a handoff message and a user message are all stored messages). Hooks run at turn start and again when a waiting turn resumes (the data may have changed while it waited; the Orchestrator keeps its status board).
- A resumed graph's messages come from its checkpoint, so the fresh hook text must replace the old system message there. `async agents.runtime.agent.resume_command(agent: Agent, summary: str | None, db_path: Path, turn_id: str) -> Command` reads the checkpoint's first message through `open_checkpointer(db_path, turn_id)` (`agents/runtime/waiting.py`, called, not edited) and `aget_tuple` on thread `turn_id`, and returns `Command(resume=True, update={"messages": [<build_messages(agent.definition, agent.project, agent.layout, summary, [], hook_context=agent.hook_context)[0] with its id set to that first message's id>]})`. LangGraph's `add_messages` replaces a message with the same id in place, so the order is unchanged and there is never a second system message, for checkpoints from before this plan and new ones alike. No checkpointed messages: plain `Command(resume=True)`. The executor reads `summary` with `store.summary` as its start path does. If LangGraph does not apply `update` together with `resume` on a waiting node, stop and open a Decision issue; do not work around it.
- `build_messages` appends `hook_context` as the last section of the system message, where the status board section was. Hook output never enters the session's stored messages.
- Existing behavior to keep: the Orchestrator's system message still contains `Status board:` and the same lines; `ListAgents` is untouched.

**Tests (stop condition):**
- First, before any other code of the task: a two-node LangGraph test with the repo's checkpointer shows `Command(resume=True, update={"messages": [SystemMessage(id=<the first message's id>, ...)]})` on an interrupted node replaces the checkpointed system message in place. If it does not, stop and open a Decision issue before anything else is built.
- Resume: the checkpoint's system message (its id as LangGraph generated it) is replaced in place on resume: the same message count, order unchanged, new hook text, one system message; a checkpoint with no messages resumes plain.
- Registry: output follows the order of `names`; two hooks run concurrently (a pair that each wait on the other's event would deadlock if sequential); a raising hook gives one line while the others still render; an unknown name gives its line; an empty-rendering hook adds nothing; `run([])` is `""`; a duplicate `register` raises; `CancelledError` from a hook propagates.
- `StatusBoardHook`: renders the labelled summary; `""` for an empty summary; unbound renders `[status_board: unavailable (not bound)]` directly.
- Catalog: `context_hooks` naming a registered hook is accepted, an unregistered one rejected; `status_board: true` gives `hook_names == ("status_board",)` and is accepted with the default `hook_names`; `context_hooks: [status_board]` together with `status_board: true` lists it once.
- Builder: hooks run with a `HookContext` carrying the project, the agent name and the trigger text; a build without `session_id` and `turn_id` runs none; the trigger text is read from the stored trigger message (`""` when none); an agent without hooks calls none; the stage 2 test (Orchestrator gets the summary, another agent does not) passes against a registry holding a `StatusBoardHook`.
- Executor: a turn's hooks see the trigger message's text; a waiting turn that resumes (through the real `SessionManager`, a spy hook whose output changes between start and resume) runs the hooks again, and the model's next request holds exactly one system message, in first position, with the new hook text; the transcript's stored messages never hold it; an agent with no hooks resumes as before.
- Full suite, checks; with the real services, the packaged Orchestrator's built agent has the status board in its `hook_context`; CI green.

**Checks:** `uv run ruff check`, `uv run ruff format`, `uv run pyright`, `uv run pytest`.

**PR:** open an early PR into `main` from `plan/stage-3-data-summary-task1` as soon as the task is done (`Closes #<task issue>`); the tracking and knowledge plans consume `HookRegistry`.

---

### Task 2: The locked DuckDB session and the table profile

**Files:** new `data/__init__.py`, `data/constants.py` (every cap and version, below), `data/duck.py` (the locked session), `data/profile.py` (`TableProfile`, `ColumnProfile`, `profile_table`), tests under `tests/data/` (`test_duck_lockdown.py`, `test_profile.py`).

**Interfaces:**
- Consumes `DuckDbSource(sql_from, allowed_directories)` (I15).
- Produces `data.duck.locked_connection(allowed: Sequence[Path]) -> duckdb.DuckDBPyConnection` (synchronous), and
  `async profile_table(source: DuckDbSource) -> TableProfile`, which runs the work in a thread (`asyncio.to_thread`) so the event loop is never blocked.
- Models (internal to `data/`): `ColumnProfile(name, type, nulls: int, distinct: int, min: str | None, max: str | None, top_values: list[str])`; `TableProfile(row_count: int, columns: list[ColumnProfile], more_columns: int, sample_columns: list[str], sample: list[list[str]], timed_out: bool)`.
- `data/constants.py` (one source for every number, all `Final`): `SAMPLE_ROWS = 20`, `TEXT_CUT = 80`, `MAX_COLUMNS = 50`, `MAX_TABLES = 20`, `TOP_VALUES = 5`, `SUMMARY_FORMAT_VERSION = 1`, `DATA_SUMMARY_MAX_CHARS = 20_000`, `CACHE_CAP_BYTES = 500 * 1024 * 1024`, `PROFILE_TIMEOUT_S = 30`. The spec fixes the first six and the cache cap; `DATA_SUMMARY_MAX_CHARS` and `PROFILE_TIMEOUT_S` are this plan's proposals (the spec says "a constant").

**Implementation notes:**
- The lock-down is security-critical; write it as one function and do not reorder it. On each new connection (`duckdb.connect(":memory:", config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})`, these two are the only config-dict settings), then these statements in this order: `SET allowed_directories=[<dir>, ...]`, `SET enable_external_access=false`, `SET lock_configuration=true`. The settings must be `SET` statements (verified on 2.0.0: the config dict does not give the same lock). Each allowed directory is `Path.resolve().as_posix()`, bound as a parameter list or quoted with single quotes doubled, never concatenated unquoted. One connection per profile call, closed in `finally`; never shared across threads.
- `sql_from` comes from the connection kind, which builds it from a validated folder and file names; this plan uses it verbatim after `FROM` and builds everything else itself, quoting column names as identifiers (`"` doubled). The table name never reaches SQL: callers resolve a `TableRef` through `Connections.tables` first (task 5).
- Queries on the locked connection, all over `(SELECT * FROM {sql_from})`:
  1. `DESCRIBE` for the columns; the first `MAX_COLUMNS` are profiled, the rest only counted in `more_columns`.
  2. One aggregate query for `count(*)` and, per profiled column, `count(*) - count(col)`, `approx_count_distinct(col)` and, for numeric and date or timestamp types (classified from the `DESCRIBE` type name: integer, decimal, float and double families; `DATE`, `TIMESTAMP*`), `min` and `max`, each cast to `VARCHAR` and cut at `TEXT_CUT`. Other types (booleans, nested, blobs) get nulls and distinct only.
  3. For each `VARCHAR` column, `SELECT col, count(*) FROM ... GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5`, values cut at `TEXT_CUT` (a null is not a top value).
  4. `SELECT * ... LIMIT 20` over the profiled columns, each cell `str()` and cut at `TEXT_CUT`, a null as `NULL`.
- Timeout: a timer calls `connection.interrupt()` after `PROFILE_TIMEOUT_S`; the result is then a `TableProfile` with `timed_out=True` and whatever was computed (at least the columns), never an exception. Any other `duckdb.Error` (a missing or unreadable file, a malformed CSV) is raised as `DomainError("table_unreadable", <credentials.scrub.error_text(exc)>, status=422)`, which the hook turns into one line and the route into a 422.
- Windows and Linux: DuckDB is imported only here and in tests. Paths go through `Path.resolve()` then `as_posix()` on both platforms (no `\\?\` prefix: use `os.path.realpath`'s result when `resolve()` adds one). The outside-path tests run on both CI platforms. A folder with a space in its name is in the fixtures.
- No file is written and no extension is loaded: `COPY ... TO`, `INSTALL` and `ATTACH` are refused by the lock-down and tested as such.

**Tests (stop condition):**
- Fixtures: a data folder with a CSV, a Parquet file (made in the test setup by an unlocked `duckdb` connection with `COPY ... TO`, no extra dependency), a JSON file, a JSON Lines file, and a subfolder of several Parquet files read as one table.
- Lock-down, each case refused by DuckDB through `locked_connection`: `read_csv` of a file outside the allowed folder; the same with `../` leaving the folder; an absolute path outside; `SET enable_external_access=true`; `SET allowed_directories=[...]`; `SET lock_configuration=false`; `INSTALL httpfs`, `LOAD httpfs`; `ATTACH` of a file; `COPY (SELECT 1) TO` a file inside the allowed folder. A read inside the folder works. Asserted: `current_setting('lock_configuration')` is true and the extension autoload settings are off.
- Profile: row count; null counts; `approx_count_distinct` within 10% on 10,000 distinct values; min and max for an integer, a double, a date and a timestamp column; top 5 of a text column, ties ordered by value, a value of 200 characters cut to 80; a boolean column has no min, max or top values.
- Caps: a 60-column table gives 50 `ColumnProfile`s and `more_columns == 10`; a 100,000-row table gives 20 sample rows; a text cell of 500 characters is cut to 80.
- Timeout: with `PROFILE_TIMEOUT_S` patched small, a source `range(10000000000000)` returns `timed_out=True` in well under a second and the connection is closed.
- Failure: a missing file and a malformed CSV give `table_unreadable` with no host path of the app's data folder in the message.
- The event loop is not blocked (a concurrent coroutine ticks while a profile runs); `duckdb.__version__ == config.DUCKDB_VERSION` (contracts owns both; this plan only reads them).
- Full suite, checks; CI green on both platforms.

**Checks:** `uv run ruff check`, `uv run ruff format`, `uv run pyright`, `uv run pytest`.

---

### Task 3: The result cache

**Files:** new `backend/src/mlops_agents/data/cache.py`, `backend/src/mlops_agents/paths.py` (`DataPaths.cache_dir(project_id)`), `tests/test_paths.py` (its case), `tests/data/test_cache.py`.

**Interfaces:**
- Consumes `TableProfile` (task 2); the constants of `data/constants.py`.
- Produces `class ProfileCache`: `__init__(paths: DataPaths, cap_bytes: int = CACHE_CAP_BYTES)`; `async get_or_compute(project_id: str, connection_id: str, table: str, data_version: str, sql_from: str, compute: Callable[[], Awaitable[TableProfile]]) -> TableProfile`; `DataPaths.cache_dir(project_id) -> Path` (`projects/<id>/cache`, validated like the other project paths, created on first write, not by `ensure`).

**Implementation notes:**
- Key: connection ID, table, `SUMMARY_FORMAT_VERSION`, the table's data version, and `DuckDbSource.sql_from` (so `Connections.update` pointing a connection at another folder never hits the old entry). One file per key, `<sha256(connection|table)[:16]>-<sha256(format|data_version|sql_from)[:16]>.json`, holding the profile's `model_dump_json` (the cache holds results, never table copies; the sample rows are the spec's 20).
- Single flight: one `asyncio.Lock` per key, so the four kickoff audits asking for the same table compute it once and the rest read the result.
- Writes are atomic: write a temporary file in the same folder, then `os.replace`. A file that does not parse (a crash, a format change) counts as a miss and is deleted. A profile with `timed_out=True` is returned but not cached (a slower run might finish next time).
- After a write, entries of the same `(connection, table)` prefix with another data version are removed (they can never hit again), then, while the folder is over the cap, the least recently used files are removed. Use is the file's modified time, touched with `os.utime` on every hit (access times are unreliable on Windows). Eviction never removes the file just written.
- Accepted and stated in the plan issue: the entries of a deleted connection stay until the cap evicts them; nothing in them is secret (they are summaries of files the user confirmed).

**Tests (stop condition):**
- A second call with the same key does not call `compute`; a different data version, a different format version, a different table and a different connection each compute again.
- Concurrent calls for one key compute once (a counting `compute` that yields); for different keys they run in parallel.
- A changed data version removes the old entry of that table and keeps the other tables'.
- Eviction past the cap (a small `cap_bytes`) removes the least recently used first; a hit protects an entry; the new entry survives; the folder size ends at or under the cap.
- A corrupt file is recomputed and replaced; no partial file is ever visible (a `compute` that raises leaves no file); a `timed_out` profile is not stored.
- `cache_dir` rejects an invalid project ID like the other path methods.
- Full suite, checks; CI green.

**Checks:** `uv run ruff check`, `uv run ruff format`, `uv run pyright`, `uv run pytest`.

---

### Task 4: The `data_summary` hook

**Files:** new `backend/src/mlops_agents/data/summaries.py` (`DataSummaries`), `data/render.py` (profile to text), `agents/hooks/data_summary.py` (`DataSummaryHook`), `agents/hooks/names.py` (`DATA_SUMMARY_HOOK = "data_summary"`), `api/services.py` (its lines), tests under `tests/data/` and `tests/agents/hooks/test_data_summary.py`.

**Interfaces:**
- Consumes `Connections` (`list`, `tables`, `data_version`, `duckdb_source`; I15), `ConnectionView`, `TableRef`, `TableDetail`; `ProfileCache`, `profile_table`; `HookContext`, `ContextHook`, `HookRegistry`.
- Produces `class DataSummaries`: `__init__(connections: Connections, cache: ProfileCache)`; `async table_detail(project_id: str, connection_id: str, table: TableRef) -> TableDetail` (the profile through the cache; `TableDetail.summary` is `render_table(profile)`, `columns` are the profile's `ColumnInfo(name, type)`, `row_count`, `data_version`); `async profile(project_id, connection_id, table: TableRef) -> TableProfile`.
- Produces `data.render.render_table(ref: TableRef, profile: TableProfile) -> str`, and `DataSummaryHook` (`name == "data_summary"`), registered in `services.py` before the catalog loads.

**Implementation notes:**
- Render for the hook, per project: a line `Data connections (read-only, mounted in the sandbox at /data/<connection name>):`, then per connection its name, kind and status, then per table (at most `MAX_TABLES`; the rest are named in one line, `and N more tables: a, b, c`) the `render_table` text: name, format and files, row count, data version, then one line per column (`name type, nulls, ~distinct, min..max or top values`), the `more_columns` count, and the sample rows as compact lines (`SAMPLE_ROWS`, every cell already cut at `TEXT_CUT`). A `timed_out` profile says so in one line.
- One size cap, `DATA_SUMMARY_MAX_CHARS`, for the whole hook: tables are profiled and rendered in order, and once the next table's text would pass the cap, that table and the rest are listed by name and format only ("detail left out: size cap"). Profiles are not computed for tables that will only be listed. A single table whose own text exceeds the whole cap is listed without detail too (a 50-column table with 20 full-width rows can reach tens of thousands of characters; its detail stays reachable through the route and Bash on `/data`).
- An unreachable connection (`status == "unreachable"`) contributes one line with its `status_detail`. A table that raises `table_unreadable` contributes one line with the reason; the other tables go on. Nothing raises out of `render` (the registry would report a surprise as one line anyway).
- The data version is read per table on every turn (`Connections.data_version`: a stat of the files), so a changed file recomputes at the next turn; the cache decides hit or miss.
- No connection: `render` returns `""`.
- `services.py`: build `ProfileCache(paths)` and `DataSummaries(connections, cache)`, register `DataSummaryHook` on the registry before `AgentCatalog.load`. `connections` is the service the connections plan wires; this plan adds only its own lines.

**Tests (stop condition):** with the real `ConnectionsService` and `LocalFolderKind` (connections task 1) over temp data folders, and a spy on `profile_table` (no fake `Connections`: folder-to-table discovery lives in one place):
- The rendered text names each connection, its `/data/<name>` path, its tables, schemas, row counts, stats and 20 sample rows; with two connections both appear.
- 21 tables give 20 with detail and one named; a very wide table shows 50 columns and the count of the rest.
- With a small patched cap, the tables past it are listed without detail, and `profile_table` was not called for them.
- A second render calls `profile_table` zero times; after a file's content changes (size or mtime), that table alone recomputes.
- Four concurrent `run(["data_summary"], ctx)` calls profile each table once.
- An unreachable connection gives its one line; a corrupt CSV gives one line and the other tables render; no connection gives `""`.
- The event loop stays responsive during a cold render.
- Through the registry: a hook failure is one line and does not fail the call.
- A built agent whose definition names `data_summary` has the summary in `hook_context` (the executor path from task 1, with the real registry).
- Full suite, checks; CI green on both platforms.

**Checks:** `uv run ruff check`, `uv run ruff format`, `uv run pyright`, `uv run pytest`.

---

### Task 5: The table-detail route

**Files:** new `backend/src/mlops_agents/api/routers/data.py` (routers are discovered from the package; no registration line), `tests/api/test_data_routes.py`, regenerated `frontend/openapi.json` (`schema.d.ts` is gitignored: regenerated locally, never committed).

**Interfaces:** Consumes `DataSummaries`, `Connections` (`list`, `tables`), `TableDetail`. Produces `GET /api/projects/{id}/connections/{con}/tables/{table}` returning `TableDetail`.

**Implementation notes:**
- Resolve in this order, so the table name never reaches SQL unchecked: the connection by ID among `Connections.list(project_id)` (else 404 `connection_not_found`, the code connections uses); `status == "unreachable"` gives 409 `connection_unreachable` carrying its `status_detail`; the table by exact name among `Connections.tables(project_id, con)` (else 404 `table_not_found`, the code connections' plan reserves for this route); then `DataSummaries.table_detail`. A `table_unreadable` from the profile becomes its 422.
- `response_model=TableDetail`. The route reads the cache like the hook does, so the Data page and the agents see the same numbers.
- Regenerate `frontend/openapi.json` with `uv run python -m mlops_agents.api.openapi_export` and the types with `npm run gen:api` locally; commit only `openapi.json`.

**Tests (stop condition):** with the real connections service behind the real app: 200 with `table`, `columns`, `row_count`, `data_version` (equal to `Connections.data_version`) and a `summary` that contains the first sample row; a second request is a cache hit (spy); an unknown project, connection and table each give their 404; an unreachable connection gives the 409; a table name that is not in the listing (`../x`, a name with a quote) gives 404 and runs no query; a corrupt file gives the 422; `uv run python -m mlops_agents.api.openapi_export --check` passes; full suite, checks; CI green.

**Checks:** `uv run ruff check`, `uv run ruff format`, `uv run pyright`, `uv run pytest`; from `frontend/`: `npm run lint`, `npm run format:check`, `npm test`, `npm run build`.

---
