# Stage 2 — UI Polish Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The lead's walkthrough fixes that are about look and feel (`docs/roadmap.md`, Stage 2):
- every clickable element shows the pointer cursor;
- focus rings follow each element's corners;
- the settings selects use the app's own component;
- provider cards have no empty space;
- markdown tables in messages have borders.

**Branch:** `plan/stage-2-ui-polish`. **Consumes:** merged code. **Produces:** the focus-style rule the overview's global constraints name.

**Read first:** the stage 2 overview (Global constraints, Files shared by these plans).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Pointer cursor and one focus style | merged code |
| 2 | Styled selects in settings | merged code |
| 3 | Compact provider cards and bordered tables | merged code |

---

### Task 1: Pointer cursor and one focus style

**Files:** `frontend/src/index.css`, `frontend/src/components/ui/*.tsx` (only their focus-ring classes). On `main` today no page file has focus classes (`git grep -nE "focus-visible:(ring|outline)|outline-none" -- frontend/src` matches only `components/ui`).

**Interfaces:** Consumes merged code. Produces the base focus rule (overview, Global constraints).

**Causes:**
- Tailwind 4.3 stopped setting `cursor: pointer` on buttons, and the app has no rule of its own.
- Two focus mechanisms compete:
  - shadcn components use `outline-none` plus a `focus-visible:ring-3` box shadow;
  - native controls (selects, radios, checkboxes, plain `<button>`s, nav links) keep the browser's blue outline, whose radius does not match the box around it.

**Implementation notes:**
- One base-layer rule in `index.css` gives the pointer cursor to `button:not(:disabled)`, `[role="button"]:not([aria-disabled="true"])`, `a[href]`, `summary`, `label[for]`, and `select` and `input[type=checkbox|radio]` when enabled. Utilities such as `cursor-default` on menu items still win.
- One base-layer focus rule: `:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }`. An outline follows the element's own `border-radius`, so it matches its corners. Set `accent-color: var(--primary)` on checkbox and radio inputs.
- Remove the per-component `focus-visible:ring-*` and `focus-visible:border-*` classes from `components/ui`. Elements that show focus with a background highlight keep `outline-none`: menu items, select items and listbox options. Radix focuses them as the pointer moves, so an outline there would appear on hover. Remove `outline-none` only where a control shows its focus with that ring. Keep `aria-invalid` styling, which is not focus.
- Hover: with the dev server running, hover each kind of control (button, nav link, select trigger, input, menu item). Confirm hover changes only color or background and never draws a ring. If a ring appears on hover from a source this task doesn't cover, open a Decision issue.

**Tests (stop condition):**
- A test reads `index.css` and asserts it contains the cursor rule and the focus rule. `git grep` over `components/ui` finds no `focus-visible:ring`.
- Existing component and page tests pass unchanged.
- Checked by hand in Chromium and stated in the PR with what was checked: Tab focus on a button, input, select trigger, nav link and checkbox shows one ring following its corners, and the pointer shows on each.
- `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 2: Styled selects in settings

**Files:** `frontend/src/pages/providers/AgentDefaults.tsx`, `frontend/src/pages/project-settings/AgentsSection.tsx`, a new `frontend/src/lib/selectNone.ts`, their tests, and the Orchestrator default-model step of `frontend/e2e/flow.spec.ts` (`selectOption` does not drive a Radix Select: open it and pick the option by role).

**Interfaces:** Consumes merged code (`components/ui/select.tsx`, unused today). Produces none.

**Implementation notes:**
- Replace the native `<select>` in `AgentDefaults.tsx` (default model per agent) and `AgentsSection.tsx` (per-project model override) with `components/ui/select.tsx`.
- Radix Select rejects `value=""`, so "None" and "Use the app default" use one sentinel value. `lib/selectNone.ts` holds the sentinel and the two functions that map it to and from `null`. Both files use it.
- The native checkbox in `AgentsSection.tsx` keeps its element; task 1's `accent-color` styles it.
- The other two native selects belong to other plans: `AddModelDialog.tsx` (pickers) and `AgentsPage.tsx` (chat-layout).

**Tests (stop condition):**
- Choosing a model, then "None" or "Use the app default", sends the same requests as today's tests expect (address, then `null`).
- The select is reachable by its label (`getByRole("combobox", { name })`).
- `npm test`, lint, format check, build, `npm run build && npm run e2e`; CI green, E2E included.

---

### Task 3: Compact provider cards and bordered tables

**Files:** `frontend/src/pages/providers/ProviderCard.tsx`, `frontend/src/pages/agents/MessageView.tsx`, their tests.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- Provider card:
  - Use `<Card size="sm">` (16px spacing instead of 24px).
  - Put "Remove provider" in the card's `CardAction` slot, beside the title.
  - Put "Add key" on the same row as the key list's end.
  - The card has no empty band at its top or bottom.
- Markdown in messages:
  - Pass ReactMarkdown a `components` map that renders `table`, `thead`, `tbody`, `tr`, `th`, `td` with `components/ui/table.tsx`'s parts. The cell borders are classes in this map; `ui/table.tsx`'s defaults stay unchanged, since the models table uses them too.
  - Wide tables scroll horizontally inside the message.
  - Move the existing `a` and `pre` styles from the arbitrary `[&_a]` / `[&_pre]` classes into the same map, so message markdown has one style source.

**Tests (stop condition):**
- A message with a GFM table renders a `table` with header and body cells, and its cells carry the border class.
- Links and code blocks keep their behavior, and the existing MessageView tests pass.
- The provider card still shows name, kind badge, base URL, masked keys, Add key and Remove provider, and its existing tests pass.
- `npm test`, lint, format check, build; CI green.
