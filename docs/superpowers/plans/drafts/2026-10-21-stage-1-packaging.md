> **DRAFT. Do not implement.** Still missing: an advisor review round with no blocking issues. The lead removes this banner when the plan leaves `drafts/`.

# Stage 1 — Packaging Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Each task is one PR into `main` (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** One command builds one wheel with the UI inside; installing that wheel with `uv tool install` and running `mlops-agents start` gives a working app on Windows and Linux; and one end-to-end test drives the real UI against the fake model to prove stage 1's promise.

**Branch:** `plan/stage-1-packaging`. **Consumes:** merged code for task 1; api-foundation task 4 for task 2; every other stage 1 plan for task 3. **Produces:** `scripts/build.py`, `scripts/install_check.py`, the `package` and `e2e` CI workflows.

**Read first:** the stage overview (global constraints, file structure), spec section 1 ("Packaging"), section 8 (the end-to-end check) and "Done when".

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Build script and wheel check | merged code |
| 2 | Install check on Windows and Linux | task 1; api-foundation task 4 on `main` |
| 3 | End-to-end test through the UI | task 2; every stage 1 plan on `main` |

---

### Task 1: Build script and wheel check

**Files:**
- Create: `scripts/build.py`, `.github/workflows/package.yml`
- Test: `backend/tests/test_build_script.py`

**Interfaces:**
- Consumes: `frontend` (`npm ci`, `npm run build`, which writes `backend/src/mlops_agents/web/`), `backend/pyproject.toml` (hatchling, `artifacts = ["src/mlops_agents/web/**"]` already declared), `uv build`.
- Produces: `python scripts/build.py [--skip-npm-ci]` → prints the path of `backend/dist/mlops_agents-<version>-py3-none-any.whl` and exits 0, or exits 1 with what failed. `check_wheel(path: Path) -> list[str]` (problems found; empty means good), importable from the script for tests.

**Implementation notes:**
- Steps: `npm ci` in `frontend/` (skipped with `--skip-npm-ci` when `node_modules` is current), `npm run build`, then `uv build --wheel` in `backend/` into a clean `backend/dist/` (delete only that folder's old wheels, named exactly; never a recursive delete outside it). Each step runs with `subprocess.run(..., check=False)`; a failing step prints its name and exit code and the script exits 1.
- From frontend-foundation task 2 on, `npm run build` first regenerates the API types from `frontend/openapi.json`, which api-foundation task 1 commits. Before that file exists the build still works (no pre-script yet).
- `check_wheel` opens the wheel as a zip and requires: `mlops_agents/web/index.html` and at least one file under `mlops_agents/web/assets/`; `mlops_agents/agents/definitions/orchestrator.md`; all migrations under `mlops_agents/storage/migrations/`; the wheel tag `py3-none-any` (platform-independent); and none of `tests/`, `*.db`, `keys.enc`, `cookie_secret`, `.env*`, `node_modules/`.
- `package.yml` (on pushes to `main` and `plan/**`, and pull requests): ubuntu-latest, Node 22 and uv; `python scripts/build.py`; upload the wheel as an artifact.

**Tests (behaviors to verify):** `check_wheel` on small zips built in `tmp_path`: a good one → no problems; missing `web/index.html` → a problem naming it; a stray `tests/test_x.py` or `app.db` → a problem naming it; a platform-specific tag → a problem. The script itself is checked by CI's `package` job.

**Checks:** backend checks (`uv run pytest`, ruff, pyright) and, by hand, `python scripts/build.py` on your machine with its output in the PR.

---

### Task 2: Install check on Windows and Linux

**Files:**
- Create: `scripts/install_check.py`
- Modify: `.github/workflows/package.yml` (a second job, Windows and Linux), `backend/src/mlops_agents/cli.py` (a `--no-browser` flag for `start`)
- Test: `backend/tests/test_cli.py` (the flag)

**Interfaces:**
- Consumes: task 1's wheel; api-foundation task 4's CLI and UI serving; I4 (`/api/auth/status`, `/api/auth/token`).
- Produces: `python scripts/install_check.py <wheel>`: exit 0 when the installed app works, 1 with what failed.

**Implementation notes:**
- `--no-browser`: `start` prints the link and does not open a browser (CI has none; also useful on a headless machine).
- `install_check.py`: a temporary `MLOPS_AGENTS_HOME` and `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` (so the check never touches the user's own install or data); `uv tool install <wheel>`; start `mlops-agents start --no-browser --port <free port>` with `MLOPS_AGENTS_ACCESS_TOKEN` set to a random value; wait up to 30 s for `/api/auth/status` to answer 200; then check: `/` returns HTML that contains the built UI's script tag; `/projects/prj_x/agents` returns the same HTML (deep link); `POST /api/auth/token` with the token sets the session cookie; a guarded route without it is 401. Stop the process (terminate, then kill after 10 s), print the server's output when anything failed, and remove the temporary folders.
- The keyring: local mode opens the OS keyring in the lifespan. On a Linux runner without a keyring backend, the app must still start (key operations fail later with the keyring's plain message). If it does not, that is a defect in api-foundation or backend-core: open a Bug issue, do not work around it here.
- CI: a job matrix ubuntu-latest and windows-latest that downloads task 1's wheel artifact and runs the check.

**Tests:** `start --no-browser` prints the link and never calls `webbrowser.open`. The install check itself runs in CI on both systems.

**Checks:** backend checks; the CI `package` workflow green on both systems; `python scripts/install_check.py backend/dist/<wheel>` once by hand, output in the PR.

---

### Task 3: End-to-end test through the UI

**Files:**
- Create: `frontend/e2e/stage1.spec.ts`, `frontend/playwright.config.ts`, `.github/workflows/e2e.yml`
- Modify: `frontend/package.json` (`@playwright/test` as a dev dependency, script `"e2e": "playwright test"`), `backend/tests/fakes/fake_openai.py` (a `__main__` entry: `python -m tests.fakes.fake_openai --port N` serves the fake until stopped)

**Interfaces:**
- Consumes: task 2's install check flow (reuse its start-and-wait code by importing it, or start the app the same way); every stage 1 page through its visible labels; the fake OpenAI-compatible server.

**Implementation notes:**
- Setup (Playwright `globalSetup`): start the fake server on a free port; create a small git repository in a temporary folder (README with a known first line, one Python file, committed); build and install the wheel as task 2 does, start the app with a known access token and `--no-browser`.
- The test, through the UI only, with role and label queries (no test ids):
  1. open `/#token=<token>`; the shell appears and the address bar has no token;
  2. AI providers: add a self-hosted provider with the fake's base URL; add the model `fake-tools`; verify it; it shows as verified with tool calling and streaming; set it as the Orchestrator's default model;
  3. Add project: local folder, the temporary repository's path; the project opens;
  4. Agents: ask "What does the README say?"; a tool step for Read on `README.md` appears and can be expanded; the answer streams in and ends with "Done. First line: <the README's first line>";
  5. reload the page: the conversation is still there, the tool step included.
- CI `e2e.yml`: ubuntu-latest, on pull requests and pushes to `main`; Node 22, uv; `npx playwright install --with-deps chromium`; `npm run e2e`; upload the Playwright report when it fails.
- Selectors follow the pages' visible text. When a page's label differs from the steps above, use the page's label; when a step cannot be done through the UI, that is a defect in the page's plan: open a Bug issue.

**Tests:** the spec above passes in CI and locally (`npm run e2e` after `python scripts/build.py`).

**Checks:** frontend checks, the `e2e` workflow green.

---

## Not in this plan

- Publishing the wheel anywhere (stage 1 installs from the built file).
- A live test against a real provider through the UI (the runtime plan has a live test).
