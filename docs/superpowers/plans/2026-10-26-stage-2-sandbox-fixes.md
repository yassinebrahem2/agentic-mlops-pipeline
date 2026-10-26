# Stage 2 — Sandbox Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, interfaces, tests (its stop condition) and checks.

**Goal:** Fix bug #254, found in the review of #240: a build cancelled after it started stays `building` forever, a failed build has two wordings, and one test suppression has no reason.

**Branch:** `plan/stage-2-sandbox-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** bug #254; the stage 2 overview I12 (`EnvironmentStatus`, the `sandbox.build.*` payload `{tag, detail}`); `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Every started build ends (#254) | merged code |

---

### Task 1: Every started build ends (#254)

**Files:** `backend/src/mlops_agents/sandbox/service.py` (`_build_project_image`), `backend/tests/conftest.py` (`guard_docker_cli`), `backend/tests/sandbox/test_service_env.py`.

**Interfaces:** Consumes merged code. Produces none; `Sandbox` keeps its signatures.

**Implementation notes:**
- Once `sandbox.build.started` is recorded, every way out of `_build_project_image` sets the status to `failed` (unless the build returned a status) and records exactly one `sandbox.build.finished` or `sandbox.build.failed`. That includes a cancel: set the in-memory status to `failed` first, then record the event under `asyncio.shield`, then re-raise the `CancelledError`. Recording the end event never raises out of this path: a failure to record is logged with its traceback (shutdown must never crash, as in #253).
- One wording for a failed build, `Image build failed: <error>`, built in one place and used by both handlers; the error is logged with its traceback.
- #254 item 3 (wrong test counts in #240's description): the owner corrects that merged PR's description with the real counts; no code.
- `tests/conftest.py`: the `pyright: ignore[reportUnknownMemberType]` on `request.node` goes. Type the fixture so none is needed; if pytest's own stubs make that impossible, keep the ignore and state the reason in its comment (a third-party boundary, per `AGENTS.md`).

**Tests (stop condition):**
- A fake runner whose `build` blocks; cancel `rebuild` after `started`: the status is `failed` with the one wording, `[started, failed]` are recorded once each, and the cancel propagates.
- A cancel after `started` while the event store raises on `record`: the status is still `failed`, the failure to record is logged, the cancel propagates, and nothing else raises.
- An error raised by `build_image` and an `OSError` before it give the same `detail` wording.
- Full backend suite (with Docker stopped as well), ruff, format, pyright; CI green on every job.
