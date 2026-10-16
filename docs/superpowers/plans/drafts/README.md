# Draft plans

Plans that are written but not ready to implement live here, so any session on any machine can pick them up. Each draft opens with a banner that lists what is still missing. Nobody implements a draft, and no draft gets a plan branch or issues.

A draft leaves this folder when both of these are true:
- an advisor review round finds no blocking issues;
- every plan it depends on is merged, and its Consumes lines have been checked against that merged code.

The lead then moves it with `git mv` on its `plan/<stage>-<topic>` branch, from `drafts/` to `docs/superpowers/plans/`, removes the banner, and opens its issues. Because the move happens on the branch, merging that branch takes the draft off `main`, and only one copy ever exists.

## Stage 1

| Draft | Depends on | Still missing |
|---|---|---|
| [providers](2026-10-16-stage-1-providers.md) | backend-core | Advisor rounds after round 1. Check against merged backend-core. `agent_model_defaults.model_address` has no foreign key, so an app-level default can point at a deleted model: make sure the plan shows the same clear "choose another model" message the spec requires for a project override. |
| [github-workspace](2026-10-16-stage-1-github-workspace.md) | backend-core | Advisor rounds after round 1. Check against merged backend-core. |

### Not written yet

agent-runtime, api, frontend-app and packaging have no draft. The overview gives what each one covers. When the original single stage 1 plan was split, it held this task outline for them. It is a starting point, not a decision:

| Plan | Tasks from the original outline |
|---|---|
| agent-runtime | Agent settings and model assignment. Message store, context, compaction. Model caller, LangGraph loop, turn executor. Stream hub and session manager (queue, Stop, recovery). |
| api | API foundation, login for both run modes, CLI. Projects and app info. Providers, models, agents. GitHub. Sessions and SSE. Committed OpenAPI contract. |
| frontend-app | Frontend foundation (API client from the contract, app shell, router). Projects and Add project. AI providers. Integrations and app Settings. Agents chat. Project Settings. |
| packaging | Build script, wheel contents check, end-to-end test with the fake provider, install check on Windows and Linux. |

Files that several api and frontend-app tasks edit:
- `api/services.py` and `api/app.py`: each router adds its service and router.
- `frontend/openapi.json`: regenerated after each API change.
- `frontend/src/app/router.tsx`: each page adds its route.
