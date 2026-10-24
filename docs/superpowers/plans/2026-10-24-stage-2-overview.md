# Stage 2: Agent Runtime, Full — Plans Overview

Stage 2 is built through several plans. This overview holds what they share. Read it and `docs/roadmap.md` (Stage 2) before starting any plan.

**Goal:** All seven agents on the shared runtime, with Write/Edit, Bash in a Docker sandbox, per-task git worktrees, PRs and issues, agent-to-agent messaging, the permission policy with approvals, and the audit log and event stream. Alongside it: the fixes and UI changes from the lead's stage 1 walkthrough.

**Spec:** the runtime plans follow the stage 2 spec (`docs/superpowers/specs/2026-10-24-stage-2-agent-runtime-design.md`, being written). The walkthrough plans below need no spec: each implements behavior already agreed in `docs/roadmap.md` (Stage 2, the last two bullets of "Agreed"), with the causes found in the code.

**Tech stack:** as in stage 1 (`2026-10-16-stage-1-overview.md`).

## Global constraints

- Focus styling has one owner: the base rule in `frontend/src/index.css` (ui-polish task 1). No component adds its own `focus-visible:ring-*` or `outline-*` classes.
- Value pickers use the app's components: `components/ui/select.tsx` for a fixed list, `components/ui/combobox.tsx` (pickers task 1) for a list that can be filtered or typed into. No native `<select>`.
- Every user-facing message has one source. A message the server already sends (for example `ModelEntry.problem`) is shown as sent, never rewritten in the UI.
- Frontend tests run with nothing else heavy alongside (Decision #120).

## Plans

| Plan | Branch | Covers | Consumes (besides merged code) | Starts | State |
|---|---|---|---|---|---|
| tool-errors | `plan/stage-2-tool-errors` | Bug #144 (invalid tool-call arguments dropped), a provider's rejection of a tool call goes back to the model, clearer tool schemas | none | now | Planned |
| models | `plan/stage-2-models` | Context window read from Gemini and vLLM; model warnings above the models table, rows of one height | none | now | Planned |
| ui-polish | `plan/stage-2-ui-polish` | Pointer cursor, one focus style, styled selects in settings, compact provider cards, bordered markdown tables | none | now | Planned |
| pickers | `plan/stage-2-pickers` | One combobox component; the GitHub repo picker and the model picker use it | none | now | Planned |
| chat-layout | `plan/stage-2-chat-layout` | Only the message list scrolls; a one-line composer that grows; one Send/Stop button; the styled agent switcher | none | now | Planned |
| project-context | `plan/stage-2-project-context` | App pages keep the current project; the sidebar header layout | none | now | Planned |

The runtime plans are added here once the spec is approved.

## Files shared by these plans

Each file below is edited by one plan only:
- `index.css`, the `components/ui/*` ring classes, `ProviderCard.tsx`, `AgentDefaults.tsx`, `AgentsSection.tsx`, `MessageView.tsx`: ui-polish.
- `components/ui/combobox.tsx`, `GitHubTab.tsx`, `AddModelDialog.tsx`: pickers.
- `AppShell.tsx`, `AgentsPage.tsx`, `MessageList.tsx`, `Composer.tsx`: chat-layout.
- `Sidebar.tsx`, `ProjectSwitcher.tsx`, new `lib/useCurrentProject.ts` and `lib/useProjects.ts`: project-context.
- `ModelsTable.tsx`, `ProvidersPage.tsx`, `providers/service.py`: models.
- `agents/runtime/graph.py`, `model_caller.py`, `providers/errors.py`, `agents/tools/base.py`, `agents/tools/grep.py`, `tests/fakes/fake_openai.py`: tool-errors.

Two test files are shared, and each plan edits only its own steps in them:
- `frontend/e2e/flow.spec.ts`: pickers (the Add model steps), ui-polish (the Orchestrator default-model step), models (the verified model row's cells).
- `frontend/src/pages/providers/ProvidersPage.test.tsx`: ui-polish (cards, default models) and models (the table).
- `frontend/src/pages/providers/ModelsTable.test.tsx`: models (the table) and pickers (its Add model dialog test).

The e2e flow relies on these names, which every plan keeps: the app-name heading, the "AI providers", "Projects" and "Agents" links, the `log` named "Messages", the "Message Orchestrator" input and the "Send" button.

The second plan to merge brings `main` in first. Every frontend plan runs `npm run build && npm run e2e` before its PR, and E2E CI must be green.

A plan that needs a change in another plan's file opens a Decision issue.

## Interfaces between plans

These plans produce no interface another plan consumes.
