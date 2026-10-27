# Project Knowledge File: Multi-Agent MLOps System

Current state of the project, rewritten 2026-10-16. Sections: brief, decided, open, ideas, changed.
Sources: [BRIEF] project brief PDF. [USER] stated by the user. [AGREED] proposed by Claude and explicitly accepted by the user.
Anything not listed here is unknown.

## 1. Brief [BRIEF]
- Title: "Intelligent Multi-Agent System for Autonomous DevOps / MLOps Pipelines". Track chosen: MLOps [USER].
- Core question: how can intelligent agents make an MLOps pipeline more automated, adaptive, efficient, and autonomous?
- Agents monitor pipeline stages, make decisions, and act. No predefined technical solution; innovation, reasoning, autonomy and evidence-based results matter more than technical complexity.
- Expected outcomes: functional prototype; agents that analyze, decide, collaborate and act; experiments under different scenarios; comparison with a traditional/baseline approach; results on performance, reliability and limitations; analysis of AI's impact on pipeline automation and autonomy.
- Questions the brief asks: what agents observe, their tasks, decisions and actions; independent or collaborative; how they communicate; when humans intervene; how the LLM helps; whether to fine-tune; how to evaluate; how to optimize the pipeline.
- Presentation phase is finished; the project is in implementation [USER].

## 2. Decided

### Product and users
- [USER] System and pipeline agnostic: adapts to the user's pipeline; ships no fixed tests. Evaluating every possible ML model is out of scope.
- [USER] Users: MLOps engineers, data scientists, AI engineers. One user role. One installation per user, no multi-tenancy.
- [USER] Works like VS Code workspaces: install once; opening the app with no project shows the projects list. One installation manages several projects.
- [USER] Runs locally or in the cloud. Cloud hosting = the same app with the backend on a server, opened from the user's browser.
- [USER] Projects are added through a wizard page inside the app. A project links to a local repo or a remote GitHub repo (cloned by the app). On a cloud install only remote (GitHub) repos are allowed; local folders are a local-install feature (server-side folder browser + typed path).
- [USER] Objective: gather all MLOps information for a project in one place, including what MLflow shows (model performance across releases and versions, data quality, deployment status).

### Agents
- [USER] Lineup: Orchestrator, Onboarding/Diagnosis, Data Quality (validation and drift combined), Model Performance, Retraining, Deployment & Rollback, Alert Prioritization.
- [USER] All agents are full coding agents (read files, write code, run commands) except Alert Prioritization, which is workflow-like and runs in the background with no chat.
- [USER] Orchestrator is an LLM agent that works like the assistant in a claude.ai Project: the default project-level chat that knows the whole project. It stays informed through a shared status board and event stream; it does not sit between the user and other agents.
- [USER] Onboarding: reads the project, asks about the model's objectives and use cases, proposes tests and a monitoring plan, suggests data connections, and sets up tracking (connect MLflow, or open a PR adding it).
- [USER] 2026-10-27: Onboarding sets the app up for the project and hands off: it makes sure every other agent has what it needs to start working correctly. After the handoff, the other agents start on their own work (for example Data Quality explores the data and the checks already in place, such as drift and data quality tests, and finds what is good, broken or missing) and propose what to add.
- [USER] 2026-10-27: anything an MLOps engineer does, an agent must be able to do, behind permission gates or not. The target is live production systems (assumed, not deployed).
- [USER] Data Quality: decides first whether a change is bad data (block it) or real drift (inform adaptation); writes its own checks and triggers after studying the data; schedules checks; explains every flag.
- [USER] Alert Prioritization: deduplicates, ranks and routes alerts to the owner and channel.
- [USER] Retraining and Deployment act through hooks (train / evaluate / deploy / rollback commands) found or written by agents.
- [USER] Users can talk to agents to do things and configure agent schedules on the Agents page.
- [USER] Agents record what they learn in a per-project store (the Knowledge page).

### Sessions and agent communication
- [USER] One persistent session per agent per project; each agent talks mainly with the user.
- [USER] Agents talk to each other: when agent A messages agent B, the message lands in B's session labeled "from A"; B's reply goes back into A's session labeled "from B".
- [AGREED] Safeguards: hop limit and token budget per exchange; no permission escalation (B's actions still go through the policy); everything visible in Activity and on the status board.
- [USER] A message from another agent reaches the recipient mid-turn; it does not wait for the turn to end.
- [USER] Scheduled work, whether an agent's own wake-up or a recurring routine, runs in that agent's one session, labeled with its source.
- [AGREED] Long sessions: the UI keeps the full history; the agent works from a compacted summary plus Knowledge and the status board.

### Permissions, code changes and execution
- [USER] Three permission levels: suggest only, ask first, autonomous. Plus a "bypass all permissions" mode (like Claude Code's --dangerously-skip-permissions).
- [AGREED] Permissions are enforced in code, not prompts: each tool has a risk class (read, write code, run, open issue/comment, deploy) and a policy maps risk class x level to allow / ask / deny. Bypass skips approvals but keeps the sandbox and the audit log; off by default, per project, with a warning.
- [USER] Agents never edit the main branch. At the end of each turn the runtime reminds the agent when its work may need a PR, and the agent decides whether to open one (an agent can stop while still waiting on something). PRs go through GitHub on the user's behalf (like the gh CLI), for solo and group projects. PR identity (user token vs bot/GitHub App) is the user's choice.
- [USER] Agents can open GitHub issues: issue when a human must act, PR when the agent has a fix (PR links "Fixes #n"). [AGREED] Search existing issues first and comment instead of duplicating; only incidents above a configurable priority become issues; labels for agent, category, priority.
- [AGREED] Local repo without a remote: agent branch, diff reviewed in the Inbox, merge on approval. [USER] The merge is built in the data folder; the user's repository only sees a fast-forward that git refuses over uncommitted changes, with no hooks run.
- [AGREED] Each agent task runs in its own git worktree; file claims enforced by the runtime keep two agents from changing the same files.
- [AGREED] The policy values are in the roadmap (the lead delegated them); a project's level defaults to `ask`. Agent commits are the user's, run no git hooks, and never include a file matching the secret denylist.
- [USER] Agent-run code executes in a Docker sandbox. Its environment is built from the repo's own dependency files.
- [AGREED] LLM out of the hot path: checks are deterministic code run by the scheduler; the LLM wakes only when something fires.

### Data access
- [AGREED] Data usually does not live with the code. Read-only data connections, managed like a registry; Onboarding suggests them from the code.
- [USER] 2026-10-27: only local files are supported as a data location for now; the code is built so PostgreSQL (and later others) can be added as one more kind without changing the rest.
- [USER] 2026-10-27: evaluation uses three cases: credit card fraud (class imbalance, model performance), synthetic drift and bad data, pipeline failures. The demo is a small project: a Kaggle dataset and a small, quick-to-train model (e.g. LightGBM) in a simple pipeline, enough to show every feature.
- [USER] 2026-10-27: the demo is a real GitHub repo simulating a live production system whose data is collected over time, so drift can be seen. [AGREED] Story: a bank's call centre sells term deposits; each morning a model ranks the day's leads by how likely they are to subscribe, the call centre calls from the top, and each call's outcome arrives later as the label. Data: UCI Bank Marketing (2008-2010, through the financial crisis). Objective: more subscriptions per call (precision at the daily call budget, lift, PR-AUC; never accuracy alone). ULB credit card fraud stays for the extreme-imbalance evaluation case only.
- [USER] 2026-10-27: the demo company already uses MLflow as part of its simulated MLOps pipeline.
- [AGREED] If predictions or labels are not saved anywhere, an agent opens a PR adding logging to a location a connection can read.
- [USER] Agents never load whole datasets. Data understanding uses standard notebook commands (head, describe, value counts, top, ...) run as hooks whose output is auto-injected into the agent's context.
- [AGREED] Hooks run on DuckDB (queries files and databases in place). Output is capped; truncated output carries a result handle the agent can page through or narrow with a new query. Results (never data) are cached per data version, with a size cap and least-recently-used eviction.

### MLOps tracking
- [USER] Integrate, don't rebuild MLflow. [AGREED] The app keeps one unified record (models, versions, runs, data versions, deployments, metrics over time) filled by adapters: MLflow first, git/CI (incl. GitHub Actions runs), and the agents' own runs. Other trackers later.

### Stats (per-project evaluation)
- [USER] The page is named "Stats". It shows how well the app performs on the user's own project, as the delta against a baseline ("the baseline = delta").
- [USER] Stats cover more than drift: data quality, drift, model performance (including extreme class imbalance), and operational incidents.
- [USER] 2026-10-27: the baseline must fit each project and its business; a fixed set of checks for every business is wrong.
- [AGREED] Baseline: naive static checks run silently in shadow on the same data (schema, nulls, ranges from training data, per-feature drift test at p<0.05, fixed accuracy threshold, fixed timeouts/failure counts), configurable per project.
- [AGREED] Ground truth: user verdicts on incidents (real / not an issue / wrong type), baseline alerts the agents stayed silent on that the user marks real, user-reported misses, and automatic confirmation when labels arrive.
- [AGREED] Metrics per category: detection (precision, catches the baseline missed, false alarms avoided, misses), speed (detection delay, time to resolution), noise (alerts per week, merge ratio), diagnosis (bad data vs drift correctness), human effort (autonomous share, approvals, acceptance rate, PRs opened/merged/closed), model impact (metric before vs after agent actions), cost (tokens and $ per agent, cost per incident), reliability (failed runs, tool errors, sandbox failures). Each number shows how many verdicts it rests on and stays "collecting data" until there are enough.
- [USER] Developer benchmarking is separate from the product and is the app developer's job: ULB credit card fraud (extreme class imbalance), a synthetic drift dataset, a pipeline telemetry dataset (operational incidents).

### Notifications
- [USER] v1: Discord and email. WhatsApp and Slack later.

### UI
- [USER] A working React UI (no mocks). Never use the Artifact tool for this project.
- [USER] App level (gear icon always reaches it): Projects (home), AI providers, Integrations (GitHub, Discord, email), Usage, Settings.
- [USER] Project level: Dashboard, Agents (one session per agent, inline approvals, schedules), Inbox (ranked incidents, approvals, PRs, issues), Models (ML model versions, metrics across releases, lineage), Data, Deployments, Runs (operational), Stats, Activity (audit), Knowledge (editable), Settings.
- [USER] Top priorities: Dashboard (overall view of the pipeline) and Agents (talk to agents).
- [AGREED] AI providers configured once at app level with a default model per agent; a project can override.

### Agent runtime
- [USER] One shared agent runtime (loop, turn queue + Stop, streaming, persistence, compaction, usage, limits, permission check, audit). Each agent is a definition file (Claude Code subagent style: frontmatter + role prompt) describing only what is special about it. Alert Prioritization = same runtime with chat off.
- [USER] Tools: Claude Code's standard tools (Read, Write, Edit, Glob, Grep, Bash) for general work; specialized tools for specialized tasks (agent messaging, PRs/issues).
- [USER] 2026-10-27: a standalone tool needs a really good reason; otherwise Bash is enough.
- [AGREED] Agent messaging tools modeled on Claude Code: ListAgents (who exists, status) and SendMessage(to, message); the message lands in the recipient's session labeled with the sender, and the reply arrives back in the sender's session.
- [USER] Stage 1: Orchestrator with read-only Read/Glob/Grep (filters enforced in the tools). Bash + Docker sandbox come in stage 2 (Bash only runs in the sandbox; the sandbox mounts a filtered view of the repo, network off by default).
- [USER] Safety limits (max steps etc.) are configurable, including off. Keys are verified when added; a key failing mid-work stops the turn with a clear message and flags the provider. Everything (tool errors included) returns to the agent's own context.

### Tech stack
- [USER] Frontend: React (Vite, TypeScript). Backend: FastAPI (Python). Streaming over server-sent events.
- [USER] Agent framework: LangGraph, used in the agent layer only.
- [USER] LLM access is provider-agnostic (base URL + key) through a models registry supporting hosted, self-hosted (Ollama, vLLM) and local models. [AGREED] Port the core of the registry from D:\SCRIPTS\Multimodal RAG Agent (providers, model entries, URL credential scrubbing, keyring key store, verify step) plus one bridge to an OpenAI-compatible chat model for LangGraph. Agents refuse models without the tool_calling trait. Local model files (GGUF via an app-managed llama.cpp server) are deferred completely; hosted and self-hosted only for now. Groq and Gemini keys from that project work and can be imported.
- [USER] No Anthropic or OpenAI keys.
- [USER] 2026-10-27: embeddings come from the models registry like chat models, as in the Multimodal RAG Agent project: any provider, hosted or self-hosted (local servers included), never a fixed list of providers.
- [USER] 2026-10-27: DuckDB may be upgraded to 2.0.
- [USER] Install/launch: one command. A Python package (`uv tool install`), `<app> start` runs FastAPI, which serves the built React UI and opens the browser. Same on a cloud VM. Docker required for the sandbox.
- [USER] App name undecided: placeholder `mlops-agents`, defined in one place.
- [USER] UI kit: shadcn/ui + Tailwind. [AGREED] shadcn style `radix-vega`, neutral base color (shadcn 4 dropped `new-york`).
- [AGREED] Frontend tests: Vitest 5.
- [USER] GitHub access: reuse the GitHub CLI login if present, otherwise a pasted fine-grained token; stored in the keyring.
- [USER/AGREED] Storage: SQLite. Layout: `<app data>/app.db` (projects, AI providers registry, connections, settings) and `<app data>/projects/<id>/` with `project.db`, the repo clone and worktrees. Nothing is written into the user's repo.
- [AGREED] Knowledge store: sqlite-vec + FTS5 (hybrid search) inside SQLite, behind a VectorStore interface. Code is read with file/search tools, not RAG.
- [AGREED] Single backend process (API + background agent work + scheduler); heavy work runs in Docker containers. Database layer written so Postgres is a configuration change.
- [AGREED] Backups use VACUUM INTO / the SQLite backup API, never a plain file copy. Keys: OS keyring locally; encrypted file with a master key from the environment on headless servers. SQLite files stay on native disk. Schedules live in the app's own tables.
- Verified on the dev machine (Python 3.12, Windows): sqlite-vec 0.1.9, FTS5, DuckDB 1.5.6, LangGraph AsyncSqliteSaver.

### Process
- [USER] Discuss first, then write specs with the brainstorming skill, then plans. Commits carry no AI attribution lines.
- [AGREED] Build split into sub-specs, in order: (1) foundation + first working agent (workspace, storage, AI providers, app shell, minimal add-project + GitHub integration, Orchestrator chatting with read-only repo tools); (2) agent runtime (all agents, sessions, agent messages, permissions, sandbox, worktrees, PRs/issues, Inbox approvals; audit log + event stream stored from here); (3) full onboarding + data access (diagnosis, data connections, DuckDB hooks, tracking adapters, Models/Data/Runs pages); (4) monitoring loop (checks, scheduler, Data Quality, Model Performance, incidents, Alert Prioritization, Discord/email, Dashboard, shadow baseline recording + verdict buttons from the first check); (5) action agents (Retraining, Deployment & Rollback, hooks, Deployments page); (6) Stats, Activity, Knowledge, Usage pages; (7) developer benchmark harness.
- [USER] The lead (yassinebrahem2) decides what is built and writes every spec, plan, task and issue; teammates implement task issues only.
- [AGREED] 2026-10-21: one owner per plan, plans depend on interfaces rather than whole plans, one PR per task into main, a teammate review then the lead's review and merge (details in AGENTS.md).
- [AGREED] 2026-10-23 (#120): frontend tests are CPU-bound in testing-library's role queries; a page test timing out under heavy concurrent local load is an environment limit, not a defect. testing-library's defaults stay (no raised timeouts); frontend tests run without other suites running alongside.
- [USER] 2026-10-26: only Critical and Important findings hold a merge (red CI, broken contract, touching the user's repo, secrets, data loss, crash at startup or shutdown, a 500 or wrong answer on a normal path). Minor findings never block: they are filed as a Bug or in the owner's next task in the same step as the merge. Re-reviews check only the last review's points and the new commits. Details in docs/reviewing-and-merging.md.
- [USER] 2026-10-27: the whole project, every stage and the evaluation included, must work on 2026-11-05; the team chooses which evaluation cases to cover.
- [USER] 2026-10-27: stages 3 and 4 are designed together. Onboarding's job is to make the monitoring loop (stage 4) ready to run well: explore the project and its production system, record what it learns, grant the other agents what they need, and get the systems ready.
- [AGREED] A roadmap doc holds the shared contracts (ID scheme, audit/event record shape, project folder layout, permission risk classes) referenced by every sub-spec.

## 3. Open
- What runs where: whether train/deploy hooks run in the sandbox, on the host, or on the user's infrastructure (GPUs, cloud credentials, network).
- Deliverable format. (Team of 6 since 2026-10-24. Deadline 2026-11-05, confirmed 2026-10-27; it was 2026-10-29.)
- Exact Model Performance, Retraining and Deployment & Rollback behaviors.
- Fine-tuning: not planned; no decision recorded.

## 4. Ideas (not adopted)
- Automation agent: reads every agent's top commands and some chats of its choosing, and automates recurring work for the agent stack (scripts, triggers, hooks). Must propose changes through the policy and PR flow, never silently.
- Resource/Cost Optimization agent and Security/Compliance agent (offered before the presentation).

## 5. Changed (superseded decisions)
- Orchestrator: was "routes tasks, resolves conflicts, enforces permission levels". Now the project-level assistant; permissions are enforced in code.
- OpenCode plugin: considered, not adopted as the runtime.
- Vector store: ChromaDB / Qdrant proposals replaced by sqlite-vec + FTS5.
- Earlier "2-week MVP" plan (Orchestrator + Onboarding + Data Quality) and early UI page list: superseded by the approved menus; no longer in a rush.
- ELEC2 dataset and the static HTML prototype: dropped.
- Notifications: WhatsApp moved out of v1.
- Data connection kinds: were local folder, S3/GCS/Azure, SQL database and MLflow. Now local files only, built so PostgreSQL can be added (2026-10-27).
- Developer benchmarks: were done later. Now part of the 2026-11-05 deliverable (2026-10-27).
- Promise to the user: was "the app never changes your working files or your checked-out branch". Now "never without your explicit approval": approving a local review fast-forwards the default branch (stage 2).
- File conflicts between agents: were prevented by the Orchestrator. Now file claims enforced in code, so it holds when the Orchestrator is idle or wrong.
- Onboarding: was "proposes tests and a monitoring plan". Now it prepares the project and hands off; the other agents audit and propose after the handoff (2026-10-27).
- PRs: were opened automatically when an agent finishes. Now the agent is reminded at the end of each turn and decides, because a stopped agent may still be waiting on something.
- Frontend test runner: was Vitest 3 (pinned major). Now Vitest 5: vitest 3 depended on packages with critical advisories (tinypool, @vitest/mocker) that no 3.x release fixes.

## 6. Files
- Multi-Agent-DevOps-MLOps-Pipeline.pdf: the project brief (not in this folder).
- image.jpg: the final slide screenshot (not in this folder).
