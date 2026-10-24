# Stage 2 — Pickers Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** A searchable picker uses one input that both filters the options and holds the final value (`docs/roadmap.md`, Stage 2). Today the GitHub repo picker and the model picker show an empty field. Once options load, a second "Filter" box appears with a list under it.

**Branch:** `plan/stage-2-pickers`. **Consumes:** merged code. **Produces:** `components/ui/combobox.tsx` (overview, Global constraints).

**Read first:** the stage 2 overview.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The combobox, used by the GitHub repo picker | merged code |
| 2 | The model picker | task 1 |

---

### Task 1: The combobox, used by the GitHub repo picker

**Files:** a new `frontend/src/components/ui/combobox.tsx` and its test, `frontend/src/pages/add-project/GitHubTab.tsx` and its test.

**Interfaces:** Consumes merged code (`radix-ui` Popover, `components/ui/input.tsx`). Produces:

```ts
export function Combobox(props: {
  id?: string;
  label: string;                 // accessible name
  value: string;                 // the field's text, which is the value
  onValueChange: (value: string) => void;
  options: { value: string; label?: string; detail?: string }[];
  loading?: boolean;             // options still loading: the field stays usable
  placeholder?: string;
  emptyText?: string;            // shown when no option matches
  disabled?: boolean;            // added in review of #233: the GitHub tab disables it while adding
  onKeyDown?: (event: React.KeyboardEvent<HTMLInputElement>) => void; // added in review of #233: Enter adds the typed repository
}): JSX.Element;
```

**Behavior:**
- One text input with `role="combobox"`. Typing changes the value and filters the options, matching anywhere in the value or label, case-insensitive. The options open in a popover anchored under the input.
- ArrowDown and ArrowUp move the highlight, Enter or a click picks an option (its value replaces the text), and Escape closes the list. Focus stays in the input throughout. ARIA: `aria-expanded`, `aria-controls` and `aria-activedescendant` on the input, `role="listbox"` and `role="option"` on the list.
- Typed text that matches no option is still the value: these pickers accept a typed URL, owner/name or model id.
- While `loading`, the input still takes typing and the popover shows a loading row.
- No dependency is added. Build it from `radix-ui`'s Popover (already installed) and the app's Input. Anchor the popover on the input with `Popover.Anchor`, not a Trigger, and prevent `onOpenAutoFocus`, so focus never leaves the input. Options are highlighted by background, not by DOM focus. No focus classes of its own (overview, Global constraints).

**GitHub repo picker:**
- The "Repository" field becomes the combobox. Its options are the user's repositories (`full_name`, with the description as `detail`).
- Remove the separate "Filter repositories" input and the list of buttons.

**Tests (stop condition):**
- Combobox:
  - typing filters;
  - the arrow keys and Enter pick, and Escape closes;
  - unmatched typed text is kept as the value;
  - the loading state still takes typing;
  - the ARIA attributes are present.
- GitHubTab: typing part of a repo name and pressing Enter fills the full name, and Add sends it. A pasted URL is sent as typed. There is no "Filter repositories" input anymore.
- `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 2: The model picker

**Files:** `frontend/src/pages/providers/AddModelDialog.tsx` and its test, and the Add model steps of `frontend/e2e/flow.spec.ts` (provider select, model pick).

**Interfaces:** Consumes task 1's `Combobox` and `components/ui/select.tsx`. Produces none.

**Implementation notes:**
- The provider field becomes `components/ui/select.tsx`. It replaces the native `<select>`.
- The model field becomes one `Combobox`:
  - its options are the provider's listed models (id, with the context window as `detail` when known);
  - it is usable before the listing arrives (`loading`).
- Remove the "Filter models" input, the radio list and the separate "Model ID" input.
- The context-window field and its rules are unchanged.

**Tests (stop condition):**
- Before the listing resolves (hold the stubbed response on a promise; no timers), the model field is visible and takes typing. After it resolves, typing filters the listed models and Enter picks one.
- Adding sends the chosen or typed model id and the chosen provider, as today.
- There is no "Filter models" input and no radio list.
- `npm test`, lint, format check, build, `npm run build && npm run e2e`; CI green, E2E included.
