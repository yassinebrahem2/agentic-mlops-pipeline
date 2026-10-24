# Stage 2 — Sandbox Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Bash runs only inside Docker (spec §4):
- one container per task, network off by default;
- an environment image built from the repo's own dependency files, cached by their hash;
- without Docker, every other tool still works.

**Branch:** `plan/stage-2-sandbox`. **Consumes:** I8 to I12; events task 1 (build events). **Produces:** the `Sandbox` implementation (I12), the Bash tool, and the routes `GET /sandbox`, `PUT /settings/sandbox` and `POST /sandbox/rebuild`.

**Read first:** spec §4 and §12.

**Docker in tests:** real Docker tests carry the pytest marker `docker`. They run in a new Linux CI job (`.github/workflows/backend.yml`, one job added by task 1; the Windows job runs without the marker). Every platform tests command building, template choice and the build context's contents with Docker replaced by a fake runner.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Docker detection and the docker runner | runtime-contracts task 1 |
| 2 | Environment images | task 1, events task 1 |
| 3 | Task containers | task 2 |
| 4 | Bash | task 3, runtime-contracts task 3 |

Task 3 gets an early PR: the tasks plan's task 5 calls `remove_task`.

---

### Task 1: Docker detection and the docker runner

**Files:** new `backend/src/mlops_agents/sandbox/docker.py` (the one place that runs the `docker` CLI), `sandbox/service.py` (`docker()`), `backend/src/mlops_agents/api/routers/sandbox.py` (`GET /sandbox`, Docker part), the services wiring line, `.github/workflows/backend.yml` (the Linux `docker` job), tests.

**Interfaces:** Consumes I12. Produces `Sandbox.docker` and `GET /sandbox`.

**Implementation notes:**
- Run `docker` through `asyncio.create_subprocess_exec` with an argument list, never a shell.
- `docker version --format json` decides availability. "Not installed" and "installed but the daemon is not running" get different plain messages.
- Output is decoded with replacement and scrubbed.

**Tests (stop condition):** with a fake runner, it reports available, not installed and daemon down, each with its message; on the `docker` job, a real `docker version` reports available; full suite, checks; CI green, the new job included.

---

### Task 2: Environment images

**Files:** new `sandbox/environment.py` (template choice), `sandbox/templates/*.Dockerfile` (package data), `sandbox/images.py` (build, tag, prune), `api/routers/sandbox.py` (`POST /sandbox/rebuild`, environment part of `GET /sandbox`), tests.

**Interfaces:** Consumes task 1 and `EventLog`. Produces `Sandbox.environment` and `rebuild`, and the `sandbox.build.*` events.

**Implementation notes:**
- Template choice, Python version, the tag (`<app name>-env:<project id>-<12 hex>`, from the app name's one constant), and the build context holding only the template and the dependency files: all exactly spec §4.
- Builds run with network access and a 15-minute timeout. A failed build keeps the last 50 log lines as `detail`.
- One build at a time per project. A second caller awaits the running build.
- After a new tag builds, older tags of the project that no container uses are removed.
- Environments live outside `/workspace` (`UV_PROJECT_ENVIRONMENT=/opt/venv`, and `PATH` to match).

**Tests (stop condition):**
- With a fake runner:
  - the template is chosen for each kind of repo (conda, uv, pyproject, requirements, none);
  - the Python version comes from `.python-version`, then `requires-python`, then the default;
  - the build context lists only the expected files, never a repository file;
  - the tag is stable for the same files and changes when one changes;
  - a failure gives its last 50 lines.
- On the `docker` job, a real build from a tiny `requirements.txt` succeeds, and a second call reuses it.
- Full suite, checks; CI green.

---

### Task 3: Task containers

**Files:** new `sandbox/containers.py`, `sandbox/service.py` (`remove_task`, `close`, the startup cleanup), `api/routers/sandbox.py` (`PUT /settings/sandbox`), tests.

**Interfaces:** Consumes task 2. Produces `Sandbox.remove_task`, `close`, the project setting `sandbox_network`, and `PUT /settings/sandbox`.

**Implementation notes:**
- Start options, labels, `HOME=/tmp`, uid and gid on POSIX, no host environment, and `PYTHONPATH`: exactly spec §4.
- Containers are named `<app name>-<task id>`.
- A dependency change, detected by the image tag changing at a `run`, recreates the container on the new image.
- Changing `sandbox_network` recreates the project's running containers.
- At startup, containers with the app's label and no open task are removed. `close` removes every running one.

**Tests (stop condition):** with a fake runner, the exact `docker run` arguments, including `--network none` and their absence with network on; on the `docker` job, a container sees the mounted worktree, has no network, and is gone after `remove_task`; orphans are removed at startup; full suite, checks; CI green.

---

### Task 4: Bash

**Files:** new `backend/src/mlops_agents/agents/tools/bash.py`, `sandbox/service.py` (`run`), `agents/tools/__init__.py` (its line), tests.

**Interfaces:** Consumes task 3 and I8. Produces `Sandbox.run` and the tool `Bash(command, timeout?, description?)` (risk class `run`, preview kind `command`).

**Implementation notes:**
- No current task is the tool error "Start a task first with StartTask."
- `run` makes sure the image is ready (building it on first use) and the container is running, then runs `docker exec <container> bash -lc <command>`.
- Output is captured as it streams, bounded to the first and last 15,000 characters, and the cut is noted.
- A timeout (default 120 s, at most 600 s) restarts the container (`docker restart -t 0`) and says the command was stopped.
- A non-zero exit is a normal result, carrying its exit code.
- `SandboxUnavailable` becomes the tool's error result. After the command, the task's file view is invalidated.

**Tests (stop condition):**
- With a fake runner: the bounded capture keeps the head and tail of a 1 MB output; a timeout restarts the container; a missing Docker gives the error text; no task gives the error.
- On the `docker` job: `echo`, a failing command with its exit code, a file written by Bash that Read sees afterwards, and a timeout.
- Full suite, checks; CI green.
