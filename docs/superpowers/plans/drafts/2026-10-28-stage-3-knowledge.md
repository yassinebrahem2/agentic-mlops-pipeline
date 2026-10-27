> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Knowledge Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: `docs/superpowers/plans/drafts/2026-10-28-stage-3-overview.md` (or its place on `main`).

**Goal:** the project's Knowledge store (spec §8):
- notes in `project.db` with hybrid search: FTS5 on title and text, sqlite-vec on embeddings, merged by reciprocal rank fusion, behind one `VectorStore`;
- `SaveNote` and `SearchNotes` tools, the `knowledge` context hook, and the notes routes;
- a search that degrades to full text, and says why, whenever vectors cannot be used; a re-embed in the background when the embedder changes.

**Branch:** `plan/stage-3-knowledge`. **Consumes:** I14 (`Embedder`, `Embedders` with `for_project` and `status`, `EmbedderUnavailable`), I17 (the models, `VectorStore`, `Knowledge`), I18 (`ContextHook`, `HookContext` from contracts; `HookRegistry` from data-summary); contracts task 1's "Shared by contracts" (the `kno` prefix, the event types `note.saved` and `note.deleted`, the `sqlite-vec` dependency, the `notes`, FTS5 and `note_embeddings` schema); embedders task 1; data-summary task 1. **Produces:** `Knowledge` (I17) as `KnowledgeService`, `SqliteVectorStore`, the tools `SaveNote` and `SearchNotes`, the `knowledge` hook, `GET/POST /notes`, `PATCH/DELETE /notes/{kno}`, the events `note.saved` and `note.deleted`, and sqlite-vec loaded into every `project.db` engine.

**Read first:** spec §8, §3's last two paragraphs (embedders never stall; search falls back to full text), §14 and §15's Knowledge lines; the stage 1 storage code (`backend/src/mlops_agents/storage/sqlite.py`, `databases.py`, `migrate.py`, `backup.py`) and `agents/approvals/settings.py` (how `project_settings` is read and written).

**Verified on this machine while writing this plan** (Python 3.13, SQLite 3.50.4, sqlite-vec 0.1.9, SQLAlchemy 2.1.4, aiosqlite 0.22.1, a path with spaces): the extension loads into an aiosqlite connection from SQLAlchemy's `connect` event through `dbapi_connection.run_async(...)` with `driver_connection.enable_load_extension` and `load_extension(sqlite_vec.loadable_path())`; `vec0` accepts a `TEXT PRIMARY KEY`, a `TEXT` metadata column usable in a KNN `WHERE`, and `DELETE ... WHERE <metadata> = ?`; a wrong-sized vector is an error, never a silent mix; `VACUUM INTO` (the backup) and a `DROP TABLE` / `ALTER TABLE ... RENAME` on another table work on a database holding `vec0` tables from a connection that has not loaded the extension; reading a `vec0` table without the extension fails with `no such module: vec0`; FTS5 is compiled in. `sqlite_vec` ships no type stubs (pyright strict: `reportMissingTypeStubs` on the import). `sqlite-vec` is in the venv today but not in `pyproject.toml`; contracts adds the dependency (and `uv.lock`), so this plan never edits either.

## Design decisions this plan makes (spec silent)

- **One embedder per project at a time; a note has at most one vector.** Vector rows are keyed by the embedder key `"<address>@<dimensions>"` (`embedder_key`), so the same address re-verified with another dimension is a different embedder. Replacing a note's vector replaces it in whatever table held the old one.
- **Vectors are used only when the project's vectors are ready for the current embedder.** The project setting `knowledge_vectors_ready_for` holds the embedder key whose re-embed pass last finished with nothing pending. Current key equal to it: hybrid search (a note saved later simply waits for its vector, and still ranks through FTS). Different or unset: full text only, with the reason, while a background pass fills in the vectors. This is "until it finishes, search is full-text only" (§8) without any in-memory state that a restart would lose.
- **The embedder never blocks a save.** `save` writes the note and its FTS row, then asks the background pass to embed it and returns. Embedding is never done inside a database transaction or under the project lock.
- **A note's author is who created it**; an update by someone else keeps the author and moves `updated`.
- **Notes are scrubbed before they are stored** with the app's secret sources (`credentials/scrub.py`: `scrub_url_secrets`, `scrub_values`), since agents write them from repository and tracker content. The title is scrubbed too.
- **Events.** `save` records `note.saved` and `delete` records `note.deleted` through `EventLog.record(project_id, source=<author>, type=..., subject_id=<note id>, payload={"note_id", "type", "title"})` (delete: `note_id` and `type`). The text is never put in an event. The background embedding records none.
- **Limits live in one module**, `knowledge/limits.py`: title 200 characters, text 8,000, `RRF_K = 60`, 50 candidates per ranking, embed batch 16, query cut at 2,000 characters and 32 distinct tokens for FTS, search `limit` at most 20.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | sqlite-vec in the project engines, and the `VectorStore` | contracts task 1 |
| 2 | Notes and full-text search | contracts task 1 (independent of task 1) |
| 3 | Embedding, hybrid search and re-embedding | tasks 1 and 2, embedders task 1 |
| 4 | The notes routes and the wiring | task 3 |
| 5 | `SaveNote`, `SearchNotes` and the `knowledge` hook | task 4, data-summary task 1 |

No early PR: `Knowledge` is first consumed by the onboarding plan, after every plan merges.

---

### Task 1: sqlite-vec in the project engines, and the `VectorStore`

**Files:** new `backend/src/mlops_agents/storage/vector_extension.py`; `storage/sqlite.py` (`create_engine_for(path, *, vectors: bool = False)`); `storage/databases.py` (`project()` passes `vectors=True`, `app()` does not); new `backend/src/mlops_agents/knowledge/vectors.py`; `knowledge/limits.py`; tests `tests/storage/test_vector_extension.py`, `tests/knowledge/test_vectors.py`.

**Interfaces:** Consumes I17's `VectorStore` and the contracts table `note_embeddings` (Interface gap 1). Produces `load_vector_extension(conn: aiosqlite.Connection) -> None`; `SqliteVectorStore(databases: Databases)` implementing I17's `VectorStore` (`upsert`, `remove`, `nearest`, `drop_embedder`); `VectorStoreError(Exception)` (message is user-facing); `MAX_DIMENSIONS`.

**Implementation notes:**
- `vector_extension.py` is the one place that imports `sqlite_vec`, with one inline `# pyright: ignore[reportMissingTypeStubs]` on that import, naming the reason (third-party package without stubs). It calls `sqlite_vec.loadable_path()` (no extension; SQLite appends `.dll` or `.so`), checks the file exists, and:
  ```python
  async def load_vector_extension(conn: aiosqlite.Connection) -> None:
      await conn.enable_load_extension(True)
      try:
          await conn.load_extension(sqlite_vec.loadable_path())
      finally:
          await conn.enable_load_extension(False)  # never leave extension loading on
  ```
- `create_engine_for` adds the load to the existing `connect` listener when `vectors` is true: `dbapi_connection.run_async(lambda _raw: load_vector_extension(dbapi_connection.driver_connection))`, after the pragmas. A platform without the wheel, or a Python built without `enable_load_extension` (`AttributeError`, `sqlite3.OperationalError`), must not stop the app: log one warning (module logger, no path or secret) and leave the connection without the extension; the store reports it (next note). `connect_sync` (migrations, backups) and `connect_checkpointer` stay as they are: no migration creates a `vec0` table (the per-dimension tables are created at run time), and the verified behavior above means they need no extension.
- `SqliteVectorStore` keeps every vector in `note_vec_<dim>` per project, created on first use inside the upsert transaction. `<dim>` is an `int` checked against `1..MAX_DIMENSIONS` (8,192, sqlite-vec's limit; the test confirms it against the installed version) and is the only thing ever formatted into SQL; every value is a bound parameter.
  ```sql
  CREATE VIRTUAL TABLE IF NOT EXISTS note_vec_{dim} USING vec0(
    note_id TEXT PRIMARY KEY, embedder TEXT, embedding FLOAT[{dim}] DISTANCE_METRIC=cosine)
  ```
  Vectors are serialized with `struct.pack(f"{n}f", *vector)` (no `sqlite_vec` import needed).
- `upsert(project_id, note_id, vector, embedder)`, one transaction: look up the note's `note_embeddings` row; when it exists, `DELETE` the old vector from the table of its recorded dimension; create the table if needed; `INSERT` the new vector; replace the `note_embeddings` row (`note_id`, `embedder`, `dimensions`). vec0 has no `INSERT OR REPLACE`: it is delete then insert.
- `nearest(project_id, vector, embedder, k)`: the table is chosen by `len(vector)`; when it does not exist, return `[]` (checked in `sqlite_master`, not by catching an error). Query: `SELECT note_id FROM note_vec_{dim} WHERE embedding MATCH :q AND k = :k AND embedder = :e ORDER BY distance`. A query vector therefore never meets a table of another dimension.
- `remove(project_id, note_id)`: delete the vector from the recorded table and the bookkeeping row; nothing recorded is a no-op. `drop_embedder(project_id, embedder)`: delete that embedder's vectors and bookkeeping rows, then `DROP TABLE` each affected vec table that has no rows left (the engine has the extension, so a drop works).
- Every method first runs `SELECT vec_version()` once per engine (cached per project id). Failure raises `VectorStoreError("Vector search is not available on this installation: <reason>")`. A bad vector (empty, `len` outside `1..MAX_DIMENSIONS`, non-finite value) raises `VectorStoreError`, not `ValueError`, so callers have one exception to treat as "no vectors".

**Tests (stop condition):**
- Extension, on a temporary database: with `vectors=True`, `SELECT vec_version()` starts with `v`; with the default, the same query fails with `no such function`; `Databases.project()` has it and `Databases.app()` does not; a path with spaces works; it works on every connection of the pool (open two at once).
- Missing extension: with `sqlite_vec.loadable_path` patched to a missing file, the engine still connects and migrations and the rest of the app run; `SqliteVectorStore.upsert` raises `VectorStoreError` naming that vector search is unavailable.
- Store (real sqlite-vec, no mocks): `nearest` orders by cosine similarity; a second `upsert` of the same note leaves one row; two dimensions use two tables and a query of one length never returns notes of the other; `embedder` filters (two keys in one dimension); `remove` and `drop_embedder` delete rows and drop an emptied table; a wrong or empty vector raises `VectorStoreError`; a 8,193-dimension vector is refused before any SQL.
- Backup and migration over a database with vec tables, without the extension: `backup_db` and `snapshot_all` succeed and the copy opens with the extension and answers the same `nearest`; `migrate` applies a later test migration that rebuilds another table (`CREATE ... _new`, `DROP`, `RENAME`, the stage 2 pattern).
- Full suite; `uv sync` from the lock contracts committed gives a working import on Linux and Windows (CI runs both).

**Checks:** from `backend/`: `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest`.

---

### Task 2: Notes and full-text search

**Files:** new `knowledge/notes.py` (SQL over the contracts tables `notes` and `notes_fts`), `knowledge/query.py` (FTS query building), `knowledge/fusion.py`, `knowledge/service.py` (`KnowledgeService`: `save`, `delete`, `get`, `search`, full text only for now), tests `tests/knowledge/test_notes.py`, `test_query.py`, `test_fusion.py`, `test_service_notes.py`, `tests/knowledge/fakes.py` (shared by tasks 3 and 5).

**Interfaces:** Consumes I17's `NoteDraft`, `NoteView`, `NoteType`, `SearchResult`, `Knowledge`, `VectorStore`; I14's `Embedders` (injected, unused until task 3); the contracts tables, `ids.new_id("kno")` and `EventLog`; `credentials/scrub.py`; `DomainError`. Produces `KnowledgeService(databases, embedders, vectors, events, secrets)` where `secrets` is an async callable returning the app's secret values (the `secret_source` `build_services` already builds for the audit log); the `fuse` function; limits.

**Implementation notes:**
- **Writes.** `save(project_id, author, note, note_id=None)`: check lengths against `limits.py` (a `DomainError("note_too_long", ..., status=422)` naming the cap), reject an empty title or text, scrub title, text and source, then write in one transaction (insert with a new `kno` id, or update the row by id and set `updated`; an unknown id is `DomainError("note_not_found", ..., status=404)`), then record `note.saved`. The FTS row is kept by the contracts triggers, never written here. `delete` removes the row (the triggers drop the FTS row; `note_embeddings` cascades); unknown id is the same 404 (a `note.deleted` event follows). `get` returns `NoteView | None`. A per-project `asyncio.Lock` (one dict in the service) is held across "remove the old vector, write the note" and across "check the note is unchanged, store its vector" (task 3), never across an embedder call.
- **Order on update** (a crash must fail safe): remove the note's vector with `vectors.remove`, then write the note. A crash between the two leaves a note with no vector, which the background pass fills in; the reverse order would leave a stale vector. Task 2 already does this (the service takes the vector store; with no vectors yet it is a no-op), and task 3 adds the pass that refills it.
- **FTS query building (`query.py`)** turns free text into a MATCH expression that cannot raise a syntax error and cannot be injected: tokens are `re.findall(r"\w+", text)` (Unicode word characters only), lower-cased, deduplicated in order, cut to `MAX_QUERY_TOKENS`, each wrapped as `"token"` and joined with ` OR ` (an agent's trigger message is long: AND would match nothing; bm25 ranks the notes sharing the most and rarest words first). No tokens: no FTS query, an empty ranking. The whole expression is one bound parameter.
- **Ranking:** `SELECT n.id FROM notes_fts JOIN notes n ON n.seq = notes_fts.rowid WHERE notes_fts MATCH :q [AND n.type = :type] ORDER BY bm25(notes_fts, 5.0, 1.0) LIMIT :candidates` (title weighted 5). `search` with an empty or blank query returns the newest `limit` notes (by `updated` descending), `used_vectors=False`, `detail=None` (this is the list the routes use). `limit` is clamped to `1..SEARCH_MAX_LIMIT`.
- **`fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> list[str]`:** score of an id is the sum over rankings of `1 / (k + rank)`, rank starting at 1; ties broken by the id's position in the first ranking that holds it, then by id. Pure, no I/O.
- `notes.py` returns `NoteView`s for a list of ids in the given order (one `IN` query, missing ids dropped: a vector hit for a note deleted a moment ago disappears here).
- `fakes.py`: `ConceptEmbedder(address, dimensions, concepts)` implementing `Embedder` with a deterministic bag-of-concepts vector (words of one concept group, for example `{"car", "automobile", "vehicle"}`, add to one axis), so a meaning match is testable without a model; `FakeEmbedders` implementing `Embedders` with a swappable embedder per project and a switch that makes `embed` raise `EmbedderUnavailable`; `no_secrets` for the `secrets` argument.

**Tests (stop condition):**
- Events: `note.saved` on create and update, `note.deleted` on delete, none on a refused save, and no event payload holds the note text.
- Notes: create returns a `kno_` id, equal `created` and `updated`, the caller as `author`; update by id keeps `created` and `author`, changes `updated` and the text; an unknown id gets 404 `note_not_found` on update and delete; delete removes the note; an over-long title or text, an empty title, an empty text are refused with the right code; a secret value and a `?token=...` URL in the text are masked in what is stored and returned.
- FTS: a word in the title ranks above the same word only in the text; an updated note no longer matches its old words and matches the new; a deleted note matches nothing; `type` filters; the `fts5` rebuild command (`INSERT INTO notes_fts(notes_fts) VALUES('rebuild')`) leaves results unchanged (guards the `seq` rowid link); a note's FTS row survives `VACUUM INTO` and a copy of the database (the reason contracts must give `notes` an explicit integer primary key).
- Query building: quotes, `AND`/`NOT`/`NEAR`, `*`, `-`, `:`, parentheses, an empty string and punctuation-only input never raise and never change the query's meaning (each is one quoted token or nothing); 32 tokens at most; non-ASCII words match.
- `fuse`: a worked example with the exact expected order (for `[[a, b, c], [c, a]]` the order is `a, c, b`), a single ranking returns itself, an id in one ranking only still appears, ties are deterministic, empty rankings give `[]`.
- Search with no embedder (`FakeEmbedders` returning `None`): full-text results, `used_vectors=False`, `detail` saying no embedder is set and searches match words only (or the status problem when the fake reports one).
- Full suite, checks; CI green on Linux and Windows.

**Checks:** as task 1.

---

### Task 3: Embedding, hybrid search and re-embedding

**Files:** new `knowledge/embedding.py` (`embedder_key`, `embedding_input`, `ReembedPass`), `knowledge/state.py` (the `knowledge_vectors_ready_for` project setting, over `project_settings`, written like `agents/approvals/settings.py`), `knowledge/service.py` (search and save, `kick`, `wait_idle`, `close`), tests `tests/knowledge/test_embedding.py`, `test_hybrid_search.py`, `test_reembed.py`.

**Interfaces:** Consumes task 2, task 1, I14 `Embedder`, `Embedders`, `EmbedderUnavailable`. Produces hybrid `search`, `KnowledgeService.wait_idle(project_id) -> None` (awaits the project's running pass; tests and `close` use it) and `close() -> None` (cancels running passes).

**Implementation notes:**
- `embedding_input(note) = f"{title}\n\n{text}"[:EMBED_INPUT_CHARS]`. The query to embed is the search text cut at `QUERY_CHARS`.
- **`search`** (the order is the contract):
  1. `embedder = await embedders.for_project(project_id)`. `None`: full text and no pass; `detail` is `EmbedderStatus.problem` from `embedders.status(project_id)` when set, else `"No embedder is set, so search matches words only. Choose one in the AI providers settings."`
  2. Key not equal to the ready setting: `kick(project_id)`, full text, `detail="Notes are being re-embedded with <address>; until that finishes, search matches words only."`
  3. Otherwise embed the query and call `vectors.nearest(..., k=VECTOR_CANDIDATES)`. `EmbedderUnavailable` or `VectorStoreError`: full text only, `detail` is the exception's message, `used_vectors=False`. On success `fuse([fts_ids, vector_ids])`, `used_vectors=True`, `detail=None`. A `type` filter is applied to the vector ids in SQL by `notes.py` (a type that excludes most hits can return fewer than `limit`; stated in the docstring).
  Full-text results are always computed first, so a vector failure costs nothing already computed.
- **`save`:** task 2's write, then `kick(project_id)` and return. The note is searchable by words at once and by meaning when the pass reaches it.
- **`kick(project_id)` and the pass** (single flight per project; the subtle part):
  - State per project: the running `asyncio.Task` and a `rerun` flag. `kick` with a task running sets `rerun`; otherwise it starts one. `kick` is a no-op while the last failure is younger than `RETRY_AFTER_SECONDS` (60), read from an injected `monotonic` callable (default `time.monotonic`) so tests control time.
  - The pass: `embedder = await embedders.for_project(...)`; `None` ends it. Then loop: read up to `EMBED_BATCH` notes with no `note_embeddings` row for the current key (`notes LEFT JOIN note_embeddings`, oldest first); none pending: leave the loop. Embed the batch with one `embedder.embed(...)` call, outside any lock or transaction. For each note take the project lock, re-read the note's `updated` and skip it when it changed since the batch was read (its save already cleared the vector and kicked again), else `vectors.upsert`. Every batch of work therefore commits notes one at a time and a restart loses nothing.
  - After the loop: `rerun` set means clear it and loop again (a save that raced the final check is never missed); otherwise set `knowledge_vectors_ready_for` to the current key, then `drop_embedder` for every other key still present in `note_embeddings`, then end.
  - `EmbedderUnavailable` or `VectorStoreError` anywhere in the pass: record the message and the time, end the pass without marking ready, and leave the batch pending. Nothing is retried in a loop; the next `kick` after `RETRY_AFTER_SECONDS` (from a save, a search or the hook at the next turn) tries again. The recorded message is what `search` shows as `detail` when it next falls back for that reason.
  - `close()` cancels the tasks and awaits them; a cancelled pass leaves no half state (each note is its own transaction).
- Detecting a changed embedder is lazy: the next `search` or `save` sees a key different from the ready setting and kicks. This is I17's lazy detection.

**Tests (stop condition):** (fake embedder with concept groups; real sqlite-vec and FTS5; injected `monotonic`)
- A keyword-only match (an identifier like `XJ-900`, absent from every concept group) and a meaning-only match (query `automobile`, note says `car`) both rank in a hybrid search; a note matching both ranks first; `used_vectors=True`, `detail=None`.
- A fresh project with notes saved before any embedder existed: first search is full text with the re-embed detail; after `wait_idle`, the same search is hybrid and the ready setting holds the key.
- Changing the embedder (another address, then another dimension, then the same address with a new dimension): search is full text with the re-embed detail until `wait_idle`; afterwards `note_embeddings` holds only the new key, the old `note_vec_<dim>` table is gone, a query is answered from the new table only, and no query ever raised a dimension error. Notes saved during the pass are embedded by it (the `rerun` path): a test that saves from inside the fake embedder's `embed` call.
- Update a note: its old vector is gone at once, the note is found by its new text through FTS before the pass runs, and by meaning after `wait_idle`. Delete a note: no vector row, no bookkeeping row.
- Exhausted embedder: `EmbedderUnavailable` on the query gives full-text results, `used_vectors=False`, `detail` equal to the exception's message; a save with the embedder down succeeds, the note is found by words, stays pending; no second embed call is made before `RETRY_AFTER_SECONDS` pass on the injected clock; after it and with the embedder back, `kick` fills the vector and the search is hybrid again. The embedder is never asked to stall or retry in a loop (assert the call count).
- A note edited while a pass holds its old text (edit inside the fake `embed` call): the stale vector is not stored.
- `VectorStoreError` (extension patched away): search is full text with the "not available" detail; saving still works.
- `close()` cancels a running pass cleanly; a new service over the same database resumes by finding the pending notes (restart).
- Full suite, checks; CI green on Linux and Windows.

**Checks:** as task 1.

---

### Task 4: The notes routes and the wiring

**Files:** new `backend/src/mlops_agents/api/routers/notes.py`; the router registration line in `api/app.py`; `api/services.py` (a `knowledge: Knowledge` field and its construction lines, `stack.push_async_callback(knowledge.close)`); regenerated `frontend/openapi.json` and `frontend/src/api/schema.d.ts` (`npm run gen:api`); tests `tests/api/test_notes.py` (beside the other router tests).

**Interfaces:** Consumes task 3 and the `Embedders` instance the embedders plan puts in `build_services`; `SqliteVectorStore`; `secret_source`. Produces `GET /api/projects/{id}/notes?q=&type=&limit=` returning `SearchResult`, `POST /notes` (body `NoteDraft`, author `user`, 201 with `NoteView`), `PATCH /notes/{kno}` (body `NoteUpdate`: every `NoteDraft` field optional, at least one present; router-local model like `DecisionRequest`), `DELETE /notes/{kno}` (204).

**Implementation notes:**
- Routers hold no logic: they call `services.knowledge`. `PATCH` reads the note with `Knowledge.get` (`None` is a 404 `note_not_found`), merges the given fields and calls `save(..., note_id=...)`; the author stays the creator.
- `GET` without `q` is the list (newest first); with `q` it is a search, so the page that arrives in stage 6 and the tests see one shape: `SearchResult`.
- Errors are `DomainError`s the app's handler already renders; a malformed `kno` id is a 404 like an unknown one.
- Construction order in `build_services`: `SqliteVectorStore(databases)`, then `KnowledgeService(databases, embedders, vectors, events, secret_source)`; the `knowledge` hook is registered in task 5, after data-summary's registry exists and before `AgentCatalog.load` (I18).

**Tests (stop condition):** through the app (`TestClient` pattern of the other router tests, fake embedder): create, list newest first, search with `q` and `type`, patch one field, delete, each with the expected status and body; a note in project A is invisible in project B; 404 for an unknown project, unknown note, a malformed id; 422 for an empty title, an over-long text, an empty patch; the secret-scrubbing case through the route; `npm run gen:api` leaves no diff after the regeneration commit; full suite; CI green on Linux and Windows.

**Checks:** backend checks as task 1, plus from `frontend/`: `npm run gen:api` (then no diff), `npm run lint`, `npm run build`.

---

### Task 5: `SaveNote`, `SearchNotes` and the `knowledge` hook

**Files:** new `agents/tools/save_note.py`, `agents/tools/search_notes.py`, `agents/hooks/knowledge.py`; `agents/tools/__init__.py` (`default_registry` gains `knowledge: Knowledge | None = None` and registers both tools when given; two lines in `__all__`); `api/services.py` (pass `knowledge=` to `default_registry`; register the hook on the `HookRegistry` data-summary creates, after it exists and before `AgentCatalog.load` runs (I18), since the catalog validates `context_hooks` against `registry.names()`); tests `tests/agents/tools/test_save_note.py`, `test_search_notes.py`, `tests/agents/hooks/test_knowledge_hook.py`.

**Interfaces:** Consumes task 4, I17, I18 (`ContextHook`, `HookContext`, `HookRegistry.register`; `HookRegistry` lives in `agents/hooks/registry.py`), `AgentTool`, `ToolContext`, `RiskClass.READ`. Produces the tools `SaveNote` and `SearchNotes` (both risk `read`, justified in spec §8: the notes are in the app's database and search needs the embedder's key) and `KnowledgeHook` (`name = "knowledge"`).

**Implementation notes:**
- `SaveNote(type, title, text, source?, note_id?)`: `args_model` forbids extra fields, `type` is `NoteType`. Calls `knowledge.save(ctx.project_id, ctx.agent, NoteDraft(...), note_id)`. Output: `Saved note <id> (<type>): <title>` or `Updated note <id>`; a `DomainError` becomes a `ToolFailure` with its message. The description says what belongs (facts, decisions, incidents, runbooks, documents the team wrote), what does not (code: read it with the file tools; secrets; whole tables), and that updating needs the `note_id` from a search.
- `SearchNotes(query, type?, limit?)`: `limit` default 5, at most `SEARCH_MAX_LIMIT`. Output: one block per note with its id, type, title, source and text cut at 500 characters, the whole output cut at 12,000 characters with a line saying how many notes were left out; when `used_vectors` is false and `detail` is set, a last line `Note: <detail>`; no matches: `No notes match.` plus the same line.
- `KnowledgeHook.render(ctx)`: nothing to say (blank `trigger_text`, or no notes) returns `""`; otherwise a heading line and the five best notes for `ctx.trigger_text` (`search(..., limit=5)`), each as `- [type] title (id): text cut at 300 characters`, the whole text capped at 2,500 characters. It never prints the search `detail` (the tool does; a context line on every turn with no embedder is noise). It raises nothing: `DomainError` and `SQLAlchemyError` become one line `Knowledge notes are unavailable: <message>`, per I18.
- The hook is the main caller of `kick` after an embedder change, since it runs at every turn start.

**Tests (stop condition):**
- `SaveNote`: creates and updates through the real service; invalid args (unknown type, missing title) return the tool error; a too-long text returns the cap in the error; the author is the calling agent; both tools are `read` class, and the policy gives `allow` at every level for them (`policy.decide`).
- `SearchNotes`: ranked output with ids, the `Note:` line when full text only, `No matches` case, output cap and the "left out" line, `type` filter, `limit` above the maximum refused.
- Hook: empty store and blank trigger return `""`; five notes at most, each capped, the whole capped; a failing store (the engine disposed) returns the one-line message and does not raise; the registered hook name is `knowledge` and an `AgentDefinition` listing it in `context_hooks` loads through the catalog.
- Registry: `default_registry(knowledge=...)` has both tools and `default_registry()` has neither (the stage 1 registry test stays true).
- Full suite, checks; CI green on Linux and Windows; at the end of the plan, `GET /notes`, both tools and the hook work against one running app in a smoke test with the fake provider's `/embeddings` (embedders plan) and without any embedder.

**Checks:** as task 1.

---

## Interface gaps

The gaps of the first draft are settled in the overview (I14, I17, I18, "Shared by contracts"). One detail of the schema remains open; the plan assumes it is accepted:

1. **Shape of `note_embeddings` and the FTS table** in contracts' `0004_stage3.sql`. The overview says "note, embedder, dimensions" and "FTS5 external-content table and triggers". This plan needs: `note_embeddings` keyed by `note_id` alone (`note_id TEXT PRIMARY KEY REFERENCES notes (id) ON DELETE CASCADE, embedder TEXT NOT NULL, dimensions INTEGER NOT NULL`, an index on `embedder`): one vector per note, the embedder column holding the key `"<address>@<dimensions>"`; and `notes_fts(title, text, content='notes', content_rowid='seq', tokenize='unicode61 remove_diacritics 2')` with insert, update and delete triggers on `notes`. No `vec0` table in a migration (they are per dimension, created at run time).
