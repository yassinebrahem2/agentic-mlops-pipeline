# Known technical debt

Debt we accepted on purpose (`AGENTS.md`, Code quality). Each line says where it is, why it was accepted, and what fixing it would take. A PR that adds debt lists it in its "Known debt added" section; the lead copies it here when the PR merges. Fixing an item removes its line.

| Where | What | Why accepted | Fix |
|---|---|---|---|
| `providers/failover.py` `describe_keys`, `frontend/src/pages/agents/StallCard.tsx` | The stall card splits the keys' wording with a regex; no shared fixture pins both sides (#427 item 2) | Lowered bar, 2026-10-29: the wording is stable and both sides have their own tests | A `shared/fixtures/describe-keys.json` pinned by a backend test and parsed in `StallCard.test` |
| `agents/runtime/context.py` `build_messages` | Used only by tests (#427 item 4) | Lowered bar, 2026-10-29 | Delete it; tests use `system_message` plus `history_messages` |
| `agents/runtime/sessions.py` (403 lines), `providers/service.py` (449), `tests/storage/test_stage3_schema.py` (546) | Files over the ~400-line guideline (#427 item 7) | Lowered bar, 2026-10-29 | Split each by concern, moving code without changes |
| `frontend/src/pages/providers/ProviderCard.tsx` | `COOLDOWN_MIN`/`COOLDOWN_MAX` copy the backend's `Field(ge=1, le=1440)`; `CooldownField` does not resync from a refetched provider (#427 item 8) | Lowered bar, 2026-10-29 | Drop the copy and show the server's 422 message; resync an unedited field |
| `agents/runtime/model_caller.py` `_call_once` | A provider deleted in the middle of a turn ends it with the generic failure text, not `provider_not_found`'s message (Decision #443) | Lowered bar, 2026-10-29: rare, and it fails loudly | Catch the `provider_not_found` `DomainError` there and fail the turn with its message |
