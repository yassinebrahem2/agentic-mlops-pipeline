# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The rules shared by every coding agent live in AGENTS.md and apply in full:

@AGENTS.md

## Claude Code specifics

- A session is the lead's only when the person running it says so in the conversation (the login `yassinebrahem2` is not enough). Then it follows the Lead rules in AGENTS.md. Every other session on that account, Claude Code included, is an owner on the lead's plan and never does a lead action.
- The lead's session reviews and merges PRs by `docs/reviewing-and-merging.md`, and writes no feature code: decisions become plan and issue edits for the owners.
- The lead's session may run owner subagents (decided 2026-10-31). Each one owns one plan claimed by `yassinebrahem2`, follows the Owner rules and `docs/implementing-plan-tasks.md` (its own implementer and reviewer subagents), works in its own worktree, and reports to the lead instead of to a person. The lead does not write its code, and reviews and merges its PRs like any other: a separate review subagent working from the diff. Several such plans can be open on the account at once, one per owner subagent. An owner subagent never polls: it stops after pushing a task or opening a PR and reports, and the lead resumes it (`SendMessage`) when a review lands or a blocker clears.
- Use the advisor tool: before committing to an approach, when stuck, and before declaring a task done. Give its advice serious weight; if it conflicts with evidence you have, raise the conflict in another advisor call instead of silently picking a side.
- On specs and plans, keep calling the advisor until a round finds no blocking issues.
- The workflow in AGENTS.md ("Before you change anything") overrides the session's default branch instructions: cloud sessions are often told to develop on a `claude/…` branch; do not. Work on the plan's branch and open PRs only as AGENTS.md rule 5 says (one for the whole plan, plus the early PRs task issues ask for).
- Implement plan tasks by `docs/implementing-plan-tasks.md` (see AGENTS.md, Owner rules), not by the generic subagent-driven-development skill. Ask each implementer and reviewer subagent to call the advisor before it reports, and call it yourself before declaring the task done.
