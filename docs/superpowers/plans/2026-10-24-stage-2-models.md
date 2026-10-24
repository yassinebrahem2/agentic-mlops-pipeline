# Stage 2 — Models Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The user rarely types a context window: the app reads it from every provider that reports one. Model warnings sit above the models table, and every row keeps one height (`docs/roadmap.md`, Stage 2).

**Branch:** `plan/stage-2-models`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** the stage 2 overview, and the stage 1 spec §3 (Models).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Read the context window from Gemini and vLLM | merged code |
| 2 | Warnings above the models table, rows of one height | merged code |

---

### Task 1: Read the context window from Gemini and vLLM

**Files:** `backend/src/mlops_agents/providers/service.py` (the listing parser and `list_remote_models`), its tests.

**Interfaces:** Consumes merged code. Produces none (`RemoteModel.context_window` already exists).

**What providers report** (checked against their docs):
- Groq and OpenRouter: `context_window` / `context_length` in `/models`. Read today.
- vLLM: `max_model_len` in `/v1/models` (its `ModelCard` in vLLM's OpenAI server). Not read today.
- Gemini: its OpenAI-compatible `/models` lists ids only. The window is `inputTokenLimit` in its native listing, `GET <root>/v1beta/models`.
- Ollama: its native `/api/show` gives the model's trained maximum, not the context Ollama serves (its `num_ctx` is far smaller). Using it would make compaction start too late. Ollama keeps the user-entered value.

**Implementation notes:**
- Add `max_model_len` to the fields `_ListedModel.context()` reads.
- When a provider's base URL path ends with `/v1beta/openai`, `list_remote_models` also reads the native listing at the same host's `/v1beta/models`, following `nextPageToken`. It fills the window for each listed id, matching a native `name` with or without its `models/` prefix. `add_model`'s lookup of the wire id uses the same match (one function).
  - The key goes in the `x-goog-api-key` header, never in the URL.
  - A failed native call leaves those windows empty and is logged without the key. It never fails the listing.
- Detection is by URL shape only, so a local fake at a `/v1beta/openai` path tests it.

**Tests (stop condition), with `httpx2.MockTransport`:**
- A Gemini-shaped provider: the OpenAI-compatible listing has ids only, and the native listing (two pages) has `inputTokenLimit`. Every id gets its window, and the request carries the key only in `x-goog-api-key`.
- The native call fails with a 500: the listing still returns, with empty windows.
- A vLLM-shaped listing with `max_model_len` gets its window.
- `add_model` without a window on a Gemini-shaped provider stores the native value.
- Full backend suite, ruff, format, pyright; CI green.

---

### Task 2: Warnings above the models table, rows of one height

**Files:** `frontend/src/pages/providers/ModelsTable.tsx`, `frontend/src/pages/providers/ProvidersPage.tsx`, their tests, and the model-row step of `frontend/e2e/flow.spec.ts` (overview, shared files).

**Interfaces:** Consumes merged code (`ModelEntry.problem`, `PUT /api/models/context-window`, the remote models listing). Produces none.

**Cause:** `ModelRow` renders `ContextWindowForm` (label, input, Save, and a hard-coded sentence) inside the context-window cell, and the row is `align-top`, so the whole row grows.

**Implementation notes:**
- Under the Models title, above the table, one warning `Alert` lists every model's `problem`, as the server sends it. It is hidden when no model has one.
- Each row stays one line. A model with a `problem` gets a colored marker (an amber dot with the problem as its accessible label) in its address cell.
- A model without a window shows a "Set" button in the context-window cell. It opens a small dialog with the number input and Save.
  - The dialog pre-fills the provider's listed window when the listing reports one, so the user only confirms.
- Delete the hard-coded sentence. The server's `problem` is its one source.

**Tests (stop condition):**
- With two models, one lacking a window: the warning list shows that model's `problem` text, and only its row has the marker.
- "Set" opens the dialog. Saving calls `PUT /api/models/context-window`, and the warning disappears after the refetch.
- With no problems, no warning `Alert` is rendered.
- `npm test` (nothing else heavy running), lint, format check, build, `npm run build && npm run e2e`; CI green, E2E included.
