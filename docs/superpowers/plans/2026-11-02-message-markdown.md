# Frontend: Agent Messages Render Their Markdown Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #706. Agent answers render as one block: `MessageView`'s `markdownComponents` styles only tables, links and `pre`, and Tailwind's reset removes heading sizes, list markers and paragraph margins.

**Branch:** `plan/message-markdown`. **Milestone:** Stage 2: Agent runtime. **Consumes:** merged code. **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue, its task issue and #706.

**Read first:** bug #706; `frontend/src/pages/agents/MessageView.tsx` (`markdownComponents`, the wrapper).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Headings, lists, paragraphs and code render | merged code |

---

### Task 1: Headings, lists, paragraphs and code render

**Files:** `frontend/src/pages/agents/MessageView.tsx` (or a markdown component module beside it if others reuse it); its test.

**Notes:**
- Style `h1` to `h6`, `p`, `ul`, `ol`, `li`, `blockquote`, inline `code` and `hr` in `markdownComponents`, in the app's existing type scale and colours (light and dark), compact enough for a chat column. No new dependency unless the existing components cannot do it.

**Tests (stop condition):** first, one that fails on `main`: a message with a heading, a numbered list with a nested bullet list and two paragraphs renders those elements with their list markers and spacing classes; existing message tests pass; frontend tests, lint, format, build; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
