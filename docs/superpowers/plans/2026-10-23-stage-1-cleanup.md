# Stage 1 — Cleanup Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Fix the last small defects found in stage 1 reviews: the folder browser overwriting a typed path (#137), and a missing why-comment plus two unexplained suppressions (#134).

**Branch:** `plan/stage-1-cleanup`. **Consumes:** merged code only. **Produces:** nothing other plans use.

**Read first:** bug issues #137 and #134, and `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The folder browser keeps a typed path (#137) | merged code |
| 2 | A why-comment and no unexplained suppressions (#134) | merged code |

---

### Task 1: The folder browser keeps a typed path (#137)

**Files:** `frontend/src/components/FolderBrowser.tsx`, `frontend/src/components/FolderBrowser.test.tsx`.

**Interfaces:** Consumes merged code. Produces none (the component's props are unchanged; Add project and the project settings relink both use it).

**Implementation notes:**
- Today the `useEffect` on `listing?.path` (lines 45-49) writes every new listing's path into "Folder path", so a path typed before the first listing arrives is lost.
- The box follows the listing only while the user has not edited it since the last navigation. Typing marks it edited. Every navigation the user starts clears the mark: Go with the typed path, a folder click, Up, or a root button. The listing that navigation loads then fills the box again.

**Tests (stop condition), from `frontend/`:**
- A path typed while the first listing is still loading is kept when the listing arrives. Hold the stubbed listing response on a promise the test resolves; no timers.
- After Go, a folder click, Up or a root button, the box shows the new listing's path.
- A Go with a path that does not exist keeps the typed text in the box and shows the error.
- `npm test` with nothing else heavy running, lint, format check and build pass; CI green.
- The e2e flow's "Current folder:" wait in `frontend/e2e/flow.spec.ts` stays as it is. It is a correct web-first wait, and e2e is another plan's file.

---

### Task 2: A why-comment and no unexplained suppressions (#134)

**Files:** `.github/workflows/board.yml`, `backend/tests/fakes/fake_openai.py`.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- `board.yml`: add a one-line comment above `move_if_open` saying why it checks the state: a closed issue stays Done whatever happens to the PR or Decision that names it.
- `fake_openai.py`: the only two routes, and the only suppressions in `tests/fakes`, are the two routes (`list_models`, `chat`) carry `pyright: ignore[reportUnusedFunction]` because they are registered by decorator. Register them with `app.add_api_route("/v1/models", list_models, methods=["GET"])` and `app.add_api_route("/v1/chat/completions", chat, methods=["POST"])` after their definitions instead. The functions are then used, so both suppressions go. Behavior is unchanged.

**Tests (stop condition):**
- From `backend/`: full `uv run pytest`, `uv run ruff check`, `uv run ruff format --check` and `uv run pyright` pass, and `git grep -n "pyright: ignore" -- backend/tests/fakes` finds nothing.
- actionlint 1.7.7 reports no error on `board.yml`.
- CI green.
