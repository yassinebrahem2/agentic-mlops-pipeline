# Stage 3 — Connections Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** Fix bug #409, the Minor findings of the reviews of #383 and #405.

**Branch:** `plan/stage-3-connections-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use; every public interface keeps its signature except one I15 change: `ConnectionKind.latest_data_version` becomes `read(settings) -> tuple[Findings, str | None]` (one folder check, one scan), which `list` calls through the protocol.

**Read first:** bug #409; the connections plan (`2026-10-28-stage-3-connections.md`); `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Connections review fixes (#409) | merged code |

---

### Task 1: Connections review fixes (#409)

**Files:** `backend/src/mlops_agents/sandbox/duckdb_cli.py`, `sandbox/templates/_duckdb.fragment`, `sandbox/images.py` (only if the fragment's values come from it), the module that defines `SandboxMount`, `connections/service.py`, `connections/kinds/local_folder.py` (only for the single scan), `frontend/src/pages/project-settings/ConnectionDialog.tsx`; their tests, and `backend/tests/sandbox/test_duckdb_image_docker.py`.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- **One source for the DuckDB CLI assets and digests.** `DUCKDB_CLI_ASSETS` and `DUCKDB_CLI_SHA256` in `duckdb_cli.py` are used nowhere. Either the image loader fills the fragment's `ARG`s from them (then they are the one source and a test pins the fragment to them), or delete them. Pick the first if the loader already substitutes values into templates; say which in the PR.
- **No dead type.** `SandboxMount` is used only by tests: remove it, or use it where mounts are built.
- **One path helper** in `ConnectionDialog.tsx`: the `settings.path` extraction is written twice; one function.
- **The mounts test asserts the exact binds.** In `test_duckdb_image_docker.py`, filter Docker's own entries (`/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf`, pseudo filesystems) from `/proc/mounts` and assert the host binds are exactly `/workspace` and the connection's `/data/<name>`.
- **One scan per connection in `list`.** `list` calls `findings`, then `latest_data_version`, and `validate` resolves again. `list` calls `ConnectionKind.read(settings)` (replacing `latest_data_version` on the protocol, in contracts' `connections/contract.py` and the member-count test): one folder check and one scan, giving the findings and the data version. No other kind method changes. `read` raises what the folder check raises, so `list` still shows an unreachable folder as `unreachable` (tested).
- **`mounts` resolves once per call** and fails loudly on an unreachable connection, as today.
- **The name error does not echo input.** `invalid_connection_name` puts the submitted name in its message; say what the rule is instead (the pattern in words), never the value.

**Tests (stop condition):**
- The fragment and `duckdb_cli.py` cannot drift (if kept), or the dicts are gone and nothing imports them.
- `ConnectionDialog` tests pass with the one helper.
- The Docker test fails if an extra host bind is added (checked once by hand, stated in the PR).
- `list` with two connections scans each folder once (a counting fake or a patched scan function).
- A bad connection name's error body does not contain the submitted name.
- Full backend suite, ruff, format, pyright; frontend lint, format check, tests, build; CI green on every job.
