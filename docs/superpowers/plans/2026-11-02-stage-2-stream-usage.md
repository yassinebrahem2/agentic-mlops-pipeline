# Stage 2: A Streamed Call Is Counted Once Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #703. A streamed model call's tokens are logged once per chunk that carries usage: `_reply` (`agents/runtime/model_caller.py`) reads `usage_metadata` from the merged stream chunk, and LangChain's merge adds usage across chunks. On Gemini's OpenAI-compatible endpoint the last call of each walkthrough turn was logged at 8 to 15 times its size. Usage and cost are overstated, the Stage 7 report's tokens and cost are wrong, and compaction runs early.

**Branch:** `plan/stage-2-stream-usage`. **Milestone:** Stage 2: Agent runtime. **Consumes:** merged code. **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue, its task issue and #703.

**Read first:** bug #703; `backend/src/mlops_agents/agents/runtime/model_caller.py` (`_reply` and the stream loop), `providers/bridge.py` (`stream_usage=True`), `agents/runtime/compaction.py` (what reads the last call's prompt tokens).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Usage is taken as the provider reports it | merged code |

---

### Task 1: Usage is taken as the provider reports it

**Files:** `backend/src/mlops_agents/agents/runtime/model_caller.py`; a live test under `backend/tests/` (marked `live`); tests under `backend/tests/agents/runtime/`; the fake OpenAI server only if it needs a per-chunk-usage mode.

**Notes:**
- First record what a real provider sends: a live test (`uv run pytest -m live`, your own Gemini key from the environment, never committed or printed) streams one short reply with tools and writes down which chunks carry usage and whether the values are cumulative or per-chunk. Put what you saw in the PR.
- Then take each call's usage to match that shape for every provider: usage reported cumulatively on several chunks counts once (the last report); usage reported once counts once; per-chunk deltas, if a provider sends them, still add up. Do not special-case a provider by name.
- Calls already stored keep their numbers (recorded as debt in this plan's PR, not rewritten).

**Tests (stop condition):** first, one that fails on `main`: a stream that reports cumulative usage on every chunk logs it once; a stream with usage only on the last chunk logs it once; the live test passes with a real key; backend suite, ruff, pyright; CI green.

## Known debt added

- Calls logged before this fix keep their inflated token and cost numbers (Usage and Stats show them as stored).
