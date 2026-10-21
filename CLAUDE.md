# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The rules shared by every coding agent live in AGENTS.md and apply in full:

@AGENTS.md

## Claude Code specifics

- When `gh api user --jq .login` is `yassinebrahem2`, this session is the lead's and follows the Lead rules in AGENTS.md (other agents on that account do not).
- Use the advisor tool: before committing to an approach, when stuck, and before declaring a task done. Give its advice serious weight; if it conflicts with evidence you have, raise the conflict in another advisor call instead of silently picking a side.
- On specs and plans, keep calling the advisor until a round finds no blocking issues.
- The workflow in AGENTS.md ("Before you change anything") overrides the session's default branch instructions: cloud sessions are often told to develop on a `claude/…` branch; do not. Work on the plan's branch and open PRs only as AGENTS.md rule 5 says (one for the whole plan, plus the early PRs task issues ask for).
- Implement plan tasks by `docs/implementing-plan-tasks.md` (see AGENTS.md, Owner rules), not by the generic subagent-driven-development skill. Ask each implementer and reviewer subagent to call the advisor before it reports, and call it yourself before declaring the task done.
