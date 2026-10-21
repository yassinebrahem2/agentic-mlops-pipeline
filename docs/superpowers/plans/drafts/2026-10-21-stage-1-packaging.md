> **DRAFT. Do not implement.** Still missing: an advisor review round with no blocking issues. The lead removes this banner when the plan leaves `drafts/`.

# Stage 1 — Packaging Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Each task is one PR into `main` (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** One command builds one wheel with the UI inside, and installing that wheel with `uv tool install` and running `mlops-agents start` gives a working app on Windows and Linux.

**Branch:** `plan/stage-1-packaging`. **Consumes:** merged code for task 1; api-foundation task 4 for task 2. **Produces:** `scripts/build_wheel.py`, `scripts/install_check.py` (its start-and-wait code is reused by the e2e plan), the `package` CI workflow.

**Read first:** the stage overview (global constraints, file structure), spec section 1 ("Packaging") and "Done when".

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Build script and wheel check | merged code |
| 2 | Install check on Windows and Linux | task 1; api-foundation task 4 on `main` |

---

### Task 1: Build script and wheel check

**Files:**
- Create: `scripts/build_wheel.py`, `.github/workflows/package.yml`
- Modify: `backend/pyproject.toml` (`[tool.pyright] extraPaths = ["../scripts"]` so the test imports `build_wheel` with its types; ruff and pyright also check `scripts/` from the backend folder: add `"../scripts"` to pyright's `include` and run `uv run ruff check ../scripts`, `uv run ruff format --check ../scripts` in CI's backend job)
- Test: `backend/tests/test_build_wheel.py` (`from build_wheel import check_wheel`)

**Interfaces:**
- Consumes: `frontend` (`npm ci`, `npm run build`, which writes `backend/src/mlops_agents/web/`), `backend/pyproject.toml` (hatchling, `artifacts = ["src/mlops_agents/web/**"]` already declared), `uv build`.
- Produces: `python scripts/build_wheel.py [--skip-npm-ci]` (named `build_wheel`, not `build`, so it never shadows PyPA's `build` package) → prints the path of `backend/dist/mlops_agents-<version>-py3-none-any.whl` and exits 0, or exits 1 with what failed. `check_wheel(path: Path) -> list[str]` (problems found; empty means good), importable from the script for tests.

**Implementation notes:**
- Find `npm` and `uv` with `shutil.which` (on Windows `npm` is `npm.cmd`, which `subprocess.run(["npm", ...])` without a shell cannot start); when one is missing, exit 1 with "npm was not found on PATH. Install Node 22." or the same for uv. Steps: `npm ci` in `frontend/` (skipped with `--skip-npm-ci` when `node_modules` is current), `npm run build`, then `uv build --wheel` in `backend/` into a clean `backend/dist/` (delete only that folder's old wheels, named exactly; never a recursive delete outside it). Each step runs with `subprocess.run(..., check=False)`; a failing step prints its name and exit code and the script exits 1.
- From frontend-foundation task 2 on, `npm run build` first regenerates the API types from `frontend/openapi.json`, which api-foundation task 1 commits. Before that file exists the build still works (no pre-script yet).
- `check_wheel` opens the wheel as a zip and requires: `mlops_agents/web/index.html` and at least one file under `mlops_agents/web/assets/`; `mlops_agents/agents/definitions/orchestrator.md`; all migrations under `mlops_agents/storage/migrations/`; the wheel tag `py3-none-any` (platform-independent); and none of `tests/`, `*.db`, `keys.enc`, `cookie_secret`, `.env*`, `node_modules/`.
- `package.yml` (on pushes to `main` and `plan/**`, and pull requests): ubuntu-latest, Node 22 and uv; `python scripts/build_wheel.py`; upload the wheel as an artifact.

**Tests (behaviors to verify):** `check_wheel` on small zips built in `tmp_path`: a good one → no problems; missing `web/index.html` → a problem naming it; a stray `tests/test_x.py` or `app.db` → a problem naming it; a platform-specific tag → a problem. The script itself is checked by CI's `package` job.

**Checks:** backend checks (`uv run pytest`, ruff, pyright) and, by hand, `python scripts/build_wheel.py` on your machine with its output in the PR.

---

### Task 2: Install check on Windows and Linux

**Files:**
- Create: `scripts/install_check.py`
- Modify: `.github/workflows/package.yml` (a second job, Windows and Linux)

**Interfaces:**
- Consumes: task 1's wheel; api-foundation task 4's CLI (`start --no-browser --port N`) and UI serving; I4 (`/api/auth/status`, `/api/auth/token`).
- Produces: `python scripts/install_check.py <wheel>`: exit 0 when the installed app works, 1 with what failed.

**Implementation notes:**
- `install_check.py`: a temporary `MLOPS_AGENTS_HOME` and `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` (so the check never touches the user's own install or data); `uv tool install <wheel>`; start the installed command by its full path in the temporary `UV_TOOL_BIN_DIR` (`mlops-agents.exe` on Windows; it is not on PATH), `start --no-browser --port <free port>`, with `MLOPS_AGENTS_ACCESS_TOKEN` set to a random value; wait up to 30 s for `/api/auth/status` to answer 200; then check: `/` returns HTML that contains the built UI's script tag; `/projects/prj_x/agents` returns the same HTML (deep link); `POST /api/auth/token` with the token sets the session cookie; a guarded route without it is 401. Stop the whole process tree (terminate, then kill after 10 s) and wait for it to exit before removing the temporary folders: Windows refuses to delete SQLite files that are still open. Print the server's output when anything failed.
- The keyring: local mode opens the OS keyring in the lifespan. On a Linux runner without a keyring backend, the app must still start (key operations fail later with the keyring's plain message). If it does not, that is a defect in api-foundation or backend-core: open a Bug issue, do not work around it here.
- CI: a job matrix ubuntu-latest and windows-latest that downloads task 1's wheel artifact and runs the check.

**Tests:** the install check itself, run in CI on both systems.

**Checks:** backend checks; the CI `package` workflow green on both systems; `python scripts/install_check.py backend/dist/<wheel>` once by hand, output in the PR.

---

## Not in this plan

- Publishing the wheel anywhere (stage 1 installs from the built file).
- The end-to-end test through the UI: its own plan (stage overview, e2e row), written once the page plans are on `main`.
- A live test against a real provider through the UI (the runtime plan has a live test).
