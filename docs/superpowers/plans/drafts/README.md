# Draft plans

Plans that are written but not ready to implement live here, so any session on any machine can pick them up. Each draft opens with a banner that lists what is still missing. Nobody implements a draft, and no draft gets a plan branch or issues.

A draft leaves this folder when both of these are true:
- an advisor review round finds no blocking issues;
- every interface it consumes is on `main` or listed under another plan's Produces in the stage overview, and the ones on `main` have been checked against the code.

The lead then moves it with `git mv` on `main`, from `drafts/` to `docs/superpowers/plans/`, removes the banner, and opens its issues.

## Stage 1

No stage 1 draft is waiting: providers, github-workspace and agent-tools left this folder and are merged.

### Not written yet

The remaining stage 1 plans and the interfaces between them are listed in the stage overview (`2026-10-16-stage-1-overview.md`, Plans and Interfaces between plans).

Decided in review of the stage 1 PRs, for the api-foundation plan:
- One app-wide exception handler maps `KeyStoreError` to a 503 with its message, which is already written for users. Individual services do not wrap key-store calls.
- The app owns one HTTP client library. Providers use `httpx2`, the openai SDK's client; `integrations/github/api.py` still uses `httpx`. Move it to `httpx2` so the lifespan creates one client type for both.
