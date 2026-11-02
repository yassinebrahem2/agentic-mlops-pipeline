# Demo World: The Company Writes UTF-8 on Windows Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #680. On Windows, `demo-world up` never deploys a model: the company's `train` registers version 1, then crashes printing MLflow's run link (an emoji, printed against the HTTP tracking server `up` always serves) with `'charmap' codec can't encode character '\U0001f3c3'`, so every `daily` fails with no production model. `Company._run` reads the company's output as UTF-8, but `Company.environment()` (`company.py`, ~160) never makes the company's Python write UTF-8, and on Windows Python writes to a pipe in the locale code page. Verified: the same run with `PYTHONUTF8=1` in the shell plays July with no error. Every Windows `up` is affected, the Stage 7 agents run included.

**Branch:** `plan/demo-utf8-output`. **Milestone:** Demo project. **Consumes:** merged code. **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue, its task issue and #680.

**Read first:** bug #680; `bench/demo-world/src/demo_world/company.py` (`environment`, `_run`); `up.py` (its two `environment()` callers: the `mlflow --version` build and the MLflow server); `tests/test_company.py`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The company's programs write UTF-8 | merged code |

---

### Task 1: The company's programs write UTF-8

**Files:** `bench/demo-world/src/demo_world/company.py` (`environment`); `bench/demo-world/README.md` (one line where the company's environment is described); tests under `bench/demo-world/tests/`.

**Notes:**
- `environment()` sets `PYTHONUTF8=1` on every platform, so every program the world starts from the company's environment (its CLI, the MLflow check and server, `git pull`) writes what `_run` decodes. It is the ops host's setting, never a change to the company's code; a value the shell already set is overridden (the world decodes UTF-8 whatever it is).

**Tests (stop condition):** first, one that fails on `main`: `environment()` carries `PYTHONUTF8=1`, with and without a tracking URI; a company command that prints a non-ASCII character to a pipe (a real `python -c` child run through the world's runner, not a fake) finishes ok and its output comes back intact; demo-world's suite, ruff, ruff format; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
