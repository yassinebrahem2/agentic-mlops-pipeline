# mlops-agents

A multi-agent system that runs alongside a team's ML pipeline and acts as its around-the-clock MLOps engineer. Agents onboard a project, watch its data, training, deployment and scoring, group what goes wrong into incidents, diagnose them, and act through the team's own commands, by pull request by default and behind permission gates.

`mlops-agents` (package `mlops_agents`) is a placeholder name; the app's name is not decided yet.

## What it does

- **Projects like VS Code workspaces.** Install once, then add projects through a wizard. A project links to a local repository or a GitHub repository the app clones. Each project gets its own store; nothing is written into your working files.
- **Seven agents on one shared runtime**:
  - **Orchestrator**: the project-level assistant that knows the whole project.
  - **Onboarding & Diagnosis**: reads the repository, asks about the model's objectives, proposes data connections and tracking, and hands the project off to the other agents.
  - **Data Quality**: validation and drift together; it decides whether a change is bad data or real drift.
  - **Model Performance**, **Retraining**, **Deployment & Rollback**: work through the team's own train, evaluate, deploy and rollback commands.
  - **Alert Prioritization**: background only, no chat; merges duplicate incidents and adjusts severity.
- **Agents talk to each other** with `ListAgents` and `SendMessage`, and publish what they are doing to a shared status board.
- **Safe execution.** Bash runs only inside a Docker sandbox, each agent task works in its own git worktree, and every action goes through a permission policy (`suggest`, `ask`, `autonomous`, `bypass`) and is written to an audit log.
- **Monitoring loop.** Checks and a scheduler, incidents with states and escalation, notifications by Discord and email, and a dashboard.
- **Pages over collected data**: Dashboard, Incidents, Models, Data, Runs, Deployments, Stats, Activity, Knowledge, Usage, and an Inbox for approvals and reviews.

The build stages and the contracts every stage follows are in [`docs/roadmap.md`](docs/roadmap.md); background and decisions are in [`project_knowledge.md`](project_knowledge.md).

## Architecture

| Part | Stack |
|---|---|
| `backend/` | Python 3.12+ with uv, a FastAPI modular monolith. One process serves the API, the built UI and the agent runtime; live agent output streams over SSE. Agents run on LangGraph; each agent is a markdown definition file with frontmatter. |
| `frontend/` | React, Vite and TypeScript, with Tailwind, shadcn/ui, React Router and TanStack Query. Built into `backend/src/mlops_agents/web/` and shipped inside the wheel. |
| Storage | SQLite only: `app.db` plus one `project.db` per project, in the app's data folder outside the repository. |
| `bench/` | Developer tooling outside the product: `demo-world` (a simulated company and its world) and `benchmarks`. |
| `shared/` | Fixtures both the backend and the frontend test against. |

## Requirements

- Python 3.12 or later and [uv](https://docs.astral.sh/uv/) 0.10 or later
- Node.js and npm (for the frontend)
- Docker (agents run Bash inside a sandbox container)
- An API key for a supported model provider. Development uses Groq, Gemini or Ollama, configured in the app's providers page.

## Getting started

Build the UI into the backend package, then start the app:

```bash
cd frontend && npm ci && npm run build
```

```bash
cd backend && uv sync && uv run mlops-agents start
```

The app listens on `127.0.0.1:8765` and opens your browser. Options:

| Option | Effect |
|---|---|
| `--port <n>` | listen on another port (default 8765) |
| `--no-browser` | do not open a browser window |
| `--server` | server mode (see below) |
| `--dev` | allow the Vite dev server's origins (local mode only) |

`uv run mlops-agents backup` snapshots the app's databases.

To build a portable wheel with the UI inside it: `python scripts/build_wheel.py`.

### Data folder

Runtime data (`app.db`, the `project.db` files, worktrees, the encrypted keystore) lives in the platform's user data folder. Set `MLOPS_AGENTS_HOME` to use another folder.

### Server mode

`mlops-agents start --server` binds to `0.0.0.0` and is meant to sit behind a reverse proxy. Only GitHub repositories can be added in this mode. It needs these environment variables:

| Variable | Purpose |
|---|---|
| `MLOPS_AGENTS_PASSWORD` | the password to sign in to the app |
| `MLOPS_AGENTS_MASTER_KEY` | the key that encrypts stored credentials |
| `MLOPS_AGENTS_ALLOWED_HOSTS` | comma-separated host names the app answers on |

Optional: `MLOPS_AGENTS_TRUSTED_PROXIES` (comma-separated). Secrets belong in the environment only, never in a config file.

## Development

Backend, from `backend/`:

```bash
uv sync
uv run pytest                 # tests; add -m live for tests against real services
uv run ruff check && uv run ruff format
uv run pyright                # strict mode
```

Frontend, from `frontend/`:

```bash
npm ci
npm run dev                   # hot reload; start the backend first with: uv run mlops-agents start --dev
npm test
npm run lint && npm run format:check
npm run e2e                   # Playwright; run npm run build first
npm run gen:api               # regenerate API types after the backend API changes
```

The developer tools in `bench/demo-world/` and `bench/benchmarks/` are uv projects of their own; see their READMEs.

## Contributing

The project is built by a small team, each person with their own coding agent. Work comes from plan and task issues; the full workflow, branch rules and code quality rules are in [`AGENTS.md`](AGENTS.md). Design specs are in [`docs/superpowers/specs/`](docs/superpowers/specs/) and implementation plans in [`docs/superpowers/plans/`](docs/superpowers/plans/). Commits follow Conventional Commits.
