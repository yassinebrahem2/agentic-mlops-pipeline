# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Design phase. There is no code yet, so no build, lint or test commands exist. The stage 1 spec is written and under review; implementation starts only after the user approves the spec and then the implementation plan. Update this file with real commands once the backend and frontend are scaffolded.

## What this project is

A single-user, VS Code-style workspace app for MLOps. The user connects ML projects (a local git repo, or a GitHub repo the app clones), and LLM coding agents watch the pipeline, explain problems, open PRs and issues, and act under a permission policy enforced in code. It is a university project ("Intelligent Multi-Agent System for Autonomous DevOps / MLOps Pipelines", MLOps track) built by a team of 4.

## Documents and their roles

- `project_knowledge.md`: what we know about the project (decisions, open questions, ideas, superseded decisions). Facts only: never put plans, task lists or implementation notes in it. Update it whenever the user decides something.
- `docs/roadmap.md`: the 7 build stages and the shared contracts every stage must follow (run modes, ID prefixes, data folder layout, SQLite rules, agent definition file format, tool risk classes, event/audit record shapes). A spec that needs a different contract changes the roadmap first.
- `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`: one design spec per stage. Stage 1 (foundation and first agent) is `2026-10-16-stage-1-foundation-design.md`.
- `docs/superpowers/plans/`: implementation plans, written only after a spec is approved.

## Planned architecture (from the stage 1 spec)

- `backend/`: Python package (uv), placeholder name `mlops_agents` / CLI `mlops-agents`; the real name is undecided and must stay defined in one place. FastAPI modular monolith: domain modules (`storage`, `workspace`, `providers`, `integrations/github`, `agents`) under one `api` layer. One process serves the API, the built UI and the agent runtime; live agent output streams over SSE.
- `frontend/`: React + Vite + TypeScript, Tailwind + shadcn/ui, React Router, TanStack Query. Built into `backend/src/mlops_agents/web/` and shipped inside the wheel.
- Storage: SQLite only. `app.db` for app-level data, one `project.db` per project under the app's data folder. Nothing is ever written into the user's working files.
- Agents: one shared runtime (LangGraph); each agent is a markdown definition file with frontmatter, like a Claude Code subagent. Tools follow Claude Code's names and argument shapes (Read, Write, Edit, Glob, Grep, Bash); Bash only runs inside a Docker sandbox (stage 2).
- LLMs: provider-agnostic registry (base URL + keys, OpenAI-compatible), ported in part from `D:\SCRIPTS\Multimodal RAG Agent`. No Anthropic or OpenAI keys are available; development uses Groq/Gemini or Ollama.

## Working rules from the user

- Never use the Artifact tool in this project. UIs are real, working code in the repo.
- The product UI is dataset-agnostic. Benchmarking on public datasets is separate developer tooling, never part of the product.
- Discuss and get explicit approval before each stage: spec, then plan, then code.
