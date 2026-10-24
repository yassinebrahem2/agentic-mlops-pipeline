# Stage 2 — Chat Layout Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The agents page follows the usual chat-app layout (`docs/roadmap.md`, Stage 2):
- only the message list scrolls, and the composer stays at the bottom;
- the input starts at one line, grows with its text up to a limit, then scrolls;
- one vertically centred button sends, and becomes Stop during a turn.

**Branch:** `plan/stage-2-chat-layout`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** the stage 2 overview, and the stage 1 spec §5 (Turns) and §6 (queued messages and Stop stay).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Only the message list scrolls | merged code |
| 2 | The composer | merged code |

---

### Task 1: Only the message list scrolls

**Files:** `frontend/src/app/shell/AppShell.tsx`, `frontend/src/pages/agents/AgentsPage.tsx`, `frontend/src/pages/agents/MessageList.tsx`, their tests.

**Interfaces:** Consumes merged code. Produces none.

**Cause:** the shell's root is `min-h-screen`, not a fixed height, so the agents page's `h-full` never resolves. The whole document scrolls, the composer scrolls away, and the message list's `overflow-y-auto` never applies.

**Implementation notes:**
- The shell's root fills the viewport exactly (`h-dvh`). Its `main` is `min-h-0 overflow-auto`, so every other page scrolls inside `main` as before.
- The agents page is a column: the header row, then the message list (`min-h-0 flex-1 overflow-y-auto`), then the composer.
- The message list keeps its stick-to-bottom behavior while new text streams. It stops sticking when the user scrolls up, and sticks again when they scroll back to the bottom. Keep today's behavior if it already does this; add it if it doesn't.
- The agent switcher's native `<select>` becomes `components/ui/select.tsx`.

**Tests (stop condition):**
- The agents page renders the message list as the only element with `overflow-y-auto` between the header and the composer, and the shell root has a fixed viewport height. Assert the layout classes: jsdom has no layout engine.
- The agent switcher is a combobox by its label and switches the agent as today.
- Checked by hand and stated in the PR: in a long conversation the page itself doesn't scroll, and the composer stays visible.
- `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 2: The composer

**Files:** `frontend/src/pages/agents/Composer.tsx` and its test.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- The textarea starts at one row and its minimum height is one line. It grows with its content up to about 8 lines (`max-h-48`), then scrolls inside. Use `field-sizing: content` with the max-height cap, since browsers without `field-sizing` stay at the cap. No JavaScript height measuring.
- One button, vertically centred against the textarea (`items-center`):
  - **Stop** while a turn is running and the text box is empty;
  - **Send** in every other case, including while a turn runs if the user has typed, so queuing a message (stage 1 spec §6) still works.
  - Each state has its own accessible name and icon. Enter sends, and Shift+Enter adds a line, as today.
- Keep the error line under the input.

**Tests (stop condition):**
- Idle: the button is Send, disabled while the text is empty.
- While busy with an empty box, the button is Stop, and clicking it calls stop. Typing while busy turns it into Send, and clicking sends the message, which is queued.
- The textarea has `rows={1}` and the max-height class.
- `npm test`, lint, format check, build, `npm run build && npm run e2e` (the names in the overview's shared-files section stay); CI green, E2E included.
