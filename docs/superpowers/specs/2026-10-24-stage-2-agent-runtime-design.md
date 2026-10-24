# Stage 2: Agent runtime, full

Date: 2026-10-24. Status: draft, awaiting the lead's review.
Contracts used here (IDs, folder layout, SQLite rules, agent definition format, risk classes, event and audit records) are in `docs/roadmap.md`; this spec changes them there first where it says so. Background: `project_knowledge.md`. Stage 1 behavior not mentioned here stays as `docs/superpowers/specs/2026-10-16-stage-1-foundation-design.md` says.

The fixes and UI changes from the lead's stage 1 walkthrough are not part of this spec: they are planned directly (stage 2 overview, plans tool-errors, models, ui-polish, pickers, chat-layout, project-context).

## Goal

The seven agents work on a project the way a careful developer would:
- they read the code and change it on their own branch, in their own worktree;
- they run commands in a Docker sandbox;
- they open pull requests and issues;
- they ask the user before risky actions, and talk to each other.

The user sees what each agent is doing, approves or rejects actions from the chat or one Inbox, and can trust that nothing escapes the permission level they set.

## Scope

In:
- Tasks: one git worktree and branch per piece of agent work, with file claims between agents.
- Tools: Write, Edit, Bash (sandbox only), StartTask, ResumeTask, OpenPullRequest, SearchIssues, OpenIssue, CommentOnIssue, ListAgents, SendMessage, ScheduleWakeup.
- The Docker sandbox, with an environment built from the repo's own dependency files.
- The permission policy, approvals that pause a turn (surviving restarts), and the audit log.
- Agent-to-agent messages, delivered mid-turn, with hop and token limits.
- Self-scheduled wake-ups.
- The event stream and the status board.
- All seven agent definitions.
- UI: the agents page for several agents, approval cards in the chat, the task bar, the Inbox page, and new project settings (permissions, sandbox, GitHub issues, messaging limits).

Out, to the stage that feeds them:
- Recurring routines need stage 4's scheduler. Like wake-ups, they will run in the agent's one session, labeled with their source.
- Alert Prioritization's behavior: nothing produces alerts until stage 4. Its definition exists now and never runs in stage 2.
- Deploy hooks are stage 5. The `deploy` row of the policy table is defined now and no stage 2 tool uses it.
- A GitHub App bot identity, and network allowlists finer than on/off.

## 1. Module layout

New and changed modules. Each owns one concern; a module that needs another's concern calls its owner.

```
backend/src/mlops_agents/
  events/                  event log, audit log, status board, project event hub
  sandbox/                 Docker detection, environment images, task containers
  agents/
    tasks/                 tasks, worktrees, file claims, committing
    policy/                the policy table and the permission gate
    approvals/             approval records, deciding, resuming
    messaging/             inbound delivery, exchanges and their limits, wake-ups
    runtime/               (stage 1) graph gains the gate and the PR reminder; executor gains waiting and resume
    tools/                 one file per tool; the registry is built with the services tools need
    definitions/           seven .md files
  integrations/github/     gains pull requests, issues and labels (REST)
```

### Tool context (changes I1's tool side)

In stage 1, `ToolContext` holds only `files`. In stage 2 it holds who is calling and where, and nothing else:

```python
@dataclass(frozen=True)
class ToolContext:
    project_id: str
    agent: str             # definition name
    session_id: str
    turn_id: str
    files: FileAccess      # the current task's worktree view, else the repository's
    task: TaskRef | None   # the session's current task (id, branch, worktree path), if any
```

A tool that needs a service gets it at construction. `default_registry(...)` takes the task, sandbox, GitHub, messaging and event services. Tools never look services up at run time, and there is no service bag. This is the opposite of a mutable context holding every service.

## 2. Tasks and worktrees

A **task** is one piece of code work by one agent. ID prefix `tsk`. It has:
- a title, the agent and session, and a branch `agent/<agent>/<slug>-<6 chars of the id>`;
- a worktree under `projects/<id>/worktrees/<tsk_...>/`;
- its base: the branch and commit it started from;
- a status: `active`, `in_review` (a PR or local review is open), `merged`, `closed` or `discarded`;
- its PR number or local review id.

**Current task.** A session has at most one current task. Write, Edit and Bash act on it. Read, Glob and Grep read the current task's worktree, or the repository when there is none.
- `StartTask(title)` creates a task and makes it current. It is refused while the current task has changes in no PR or review; the agent must open one first, or ask the user to discard the task.
- `ResumeTask(task_id)` makes an earlier `active` or `in_review` task of the same agent current again, for example to answer review comments.
- Write, Edit or Bash with no current task returns a tool error: "Start a task first with StartTask."

**Creating the worktree:**
- The base is `origin/<default branch>` for GitHub projects (the managed clone is fetched first). For local projects it is the local default branch's commit.
- The steps:
  1. `git worktree add --no-checkout -b <branch> <path> <base>`;
  2. sparse-checkout in non-cone mode that includes everything except the secret denylist;
  3. checkout.

  Tracked secret files are then absent from the worktree and marked skip-worktree, so a commit never records them as deleted. A test proves both on a real repository.
- The denylist is the one in `agents/tools/files.py` (stage 1); every user of it imports it.
- For local projects this adds a branch and worktree metadata to the user's repository's git data, as the stage 1 spec announced. Their working files and checked-out branch are untouched.

**The file view in a worktree:**
- `git ls-files --cached --others --exclude-standard` minus the denylist, never outside the root.
- The cache from stage 1 (one listing per turn) is dropped after every Write, Edit and Bash, so later reads see new files.

**File claims.** The roadmap gives this to the Orchestrator; this spec enforces it in code, so it holds even when the Orchestrator is idle or wrong.
- A task claims every path it writes (Write, Edit) or commits.
- A Write or Edit to a path claimed by another open task (`active` or `in_review`) returns a tool error naming that agent and task, and suggests SendMessage to coordinate.
- Bash can change any file, so the same check runs again when committing (§6).
- Claims end when their task is merged, closed or discarded.

**Ending a task:**
- At the start of every turn and when the Inbox opens, the runtime reads the state of the open tasks' PRs from GitHub.
- A merged or closed PR ends the task. Its container and worktree are removed (`git worktree remove`), and its local branch is deleted once merged.
- The user can discard a task from the task bar. That removes the worktree, container, branch and claims after a confirmation; an open PR is left alone.

## 3. File tools

Claude Code's names and argument shapes:
- `Write(file_path, content)`
- `Edit(file_path, old_string, new_string, replace_all=false)`

Both have risk class `write_code`.

- Paths resolve inside the current task's worktree, with the stage 1 rules (no `..`, no symlink escapes). A denylisted name is refused.
- Edit, and Write over an existing file, need that file to have been Read in the same task since its last change. Otherwise the tool error is "Read <path> first." This keeps an agent from overwriting what it never saw.
- Edit fails when `old_string` is missing, or appears more than once without `replace_all`, saying which.
- Line endings and encoding are kept: UTF-8, and a file's existing CRLF or LF.
- Both tools compute their change as a unified diff first. That diff is the approval preview (§7) and the tool's success output.

## 4. Sandbox and Bash

**Docker:**
- Bash needs Docker. The app checks `docker version` at startup and on demand from project settings.
- Without Docker, Bash returns a tool error saying Docker is needed and how to get it, and every other tool works.

**The environment image, built from the repo's own files:**
- The runtime picks a template from the task's base commit, in this order:
  1. `environment.yml` or `environment.yaml`: micromamba;
  2. `uv.lock` with `pyproject.toml`: `uv sync --frozen --no-install-project`;
  3. `pyproject.toml`: its `[project].dependencies` installed with pip;
  4. `requirements.txt`: `pip install -r`;
  5. none of these: the base image only.
- The Python version comes from `.python-version`, else the lower bound of `requires-python`, else 3.12.
- The build context holds only the template and the dependency files, never the repository, so no secret can enter an image.
- The build runs with network access, a 15-minute timeout and its own status (`sandbox.build.*` events, shown in the tool step and in project settings).
- The tag is `mlops-agents-env:<project id>-<12 hex of sha256(template + dependency files)>`. An unchanged environment is never rebuilt. When a new tag builds, the project's older tags are removed.
- A failed build is a tool error carrying the last 50 lines of the build log. The Bash call that triggered it ends there; the next Bash call tries again.

**A container per task:**
- Started on the task's first Bash call with:
  - `--network none`, unless the project allows network;
  - `--cpus 2 --memory 4g --pids-limit 512`;
  - the task's worktree mounted read-write at `/workspace` (the working directory);
  - the host user's uid and gid on Linux and macOS;
  - labels with the project and task;
  - no host environment variables.
- `PYTHONPATH=/workspace/src:/workspace`, so the project's own code imports without an install, which needs network.
- It is stopped and removed when its task ends and when the app shuts down. Containers left behind by a crash are removed at startup by label.

**Bash:**
- `Bash(command, timeout?, description?)`, risk class `run`.
- It runs `bash -lc <command>` in the task's container.
- The timeout defaults to 120 s, at most 600 s. A timeout restarts the container, which ends every process; the worktree is kept.
- The output is stdout and stderr combined, plus the exit code. Above 30,000 characters the middle is cut, with a note saying so.
- Git does not work inside the sandbox: the worktree's `.git` file points to a host path. The definitions tell agents to use the task tools for git work.

## 5. Permissions and approvals

**Levels.**
- Each project has one level: `suggest`, `ask`, `autonomous` or `bypass`. It defaults to `ask`, and changes apply from the next tool call.
- Choosing `bypass` takes a confirmation. The agents page then shows a warning bar.

**Policy table** (the roadmap's values, decided here):

| Level | read | write_code | run | message | github | deploy |
|---|---|---|---|---|---|---|
| suggest | allow | deny | deny | allow | ask | deny |
| ask | allow | ask | ask | allow | ask | ask |
| autonomous | allow | allow | allow | allow | allow | ask |
| bypass | allow | allow | allow | allow | allow | allow |

- `suggest` denies `write_code`, and the denial tells the agent to propose the change as a diff in its answer.
- The sandbox, the worktree and the audit log apply at every level, `bypass` included.
- A denial returns a tool error naming the level and the risk class. The model reads it like any other tool result.

**The gate.** The turn graph becomes:
- model → gate → tools → model, and the end of a turn goes through the PR reminder (§6).
- The gate is its own node, because LangGraph re-runs a resumed node from its start. A node with side effects before its pause would repeat them.
- For each tool call of the step, the gate decides allow, deny or ask, and writes one audit record per decision.
- For the asks, it creates approval requests: ID `apr`, keyed by the tool-call id, so a re-run finds them instead of duplicating them. Each carries the tool's preview:
  - the diff for Write and Edit;
  - the command for Bash;
  - the title, body and target for GitHub tools.
- Then it calls `interrupt()` once with every pending approval.

**Waiting:**
- When the graph pauses, the executor marks the turn `waiting`, and the agent message `waiting` (a new message status). It publishes `approval.requested`, and keeps the turn's checkpoint thread (stage 1 deleted it at every turn end).
- The session's queue holds behind a waiting turn, as behind a running one.
- On startup, `waiting` turns stay as they are.

**Deciding and resuming:**
- The user approves or rejects each request, with an optional note, from the chat card or the Inbox. Each decision writes an audit record.
- When the last request of the step is decided, the session resumes the turn with `Command(resume={approval id: decision})`, on a new checkpointer connection, so this also works after a restart.
- The tools step runs the approved calls. It answers each rejected call with the tool error "The user rejected this action" plus the note.

**Stop on a waiting turn:**
- It rejects the turn's pending approvals ("stopped by the user").
- It ends the turn `stopped` and deletes its thread.

## 6. Pull requests, issues and the end-of-turn reminder

**GitHub link:**
- A project can use GitHub when it is a GitHub project, or a local project whose `origin` is a github.com repository, and the GitHub integration is set.
- Tokens reach git only through stage 1's `GIT_ASKPASS` runner.

**Committing** (one function, used by OpenPullRequest and local review):
1. Stage everything in the worktree (`git add -A`, which respects `.gitignore`).
2. If a staged path matches the denylist, unstage everything and fail, naming the paths. The agent must delete them. Nothing secret is committed or pushed.
3. If a staged path is claimed by another open task, unstage everything and fail, naming the agent.
4. Commit as the user: the name and email of the user's git config, else their GitHub login with GitHub's noreply address. The message is the PR title. Never an AI identity.

**OpenPullRequest(title, body, fixes=[])**, risk class `github`:
- With no PR yet: commit, push the branch, and open a PR into the default branch. The body ends with a line saying which agent opened it and from which task, plus `Fixes #n` for each entry of `fixes`. The task becomes `in_review`, and the event is `pr.opened`.
- With a PR already open: commit, push, and update the title and body if they changed (`pr.updated`).
- A local project without a GitHub remote gets a local review instead: an Inbox entry with the diff against the base. Approving merges it, as the lead decides in Open question 1. Rejecting closes the task, and a system message tells the agent.

**Issues:**
- `SearchIssues(query)`: risk class `read`. It searches open issues in the project's repository.
- `OpenIssue(title, body, category, priority)`: risk class `github`.
  - `priority` is one of `low`, `medium`, `high`, `critical`. It is refused below the project's minimum, which defaults to `medium`; the tool error tells the agent to report in its session instead.
  - Labels `agent:<name>`, `category:<category>` and `priority:<priority>` are created when missing.
  - The definitions tell agents to search first and comment on a match.
- `CommentOnIssue(number, body)`: risk class `github`.

**The PR reminder:**
- When the model ends a turn and the current task has changes in no PR or review (uncommitted, or committed but not pushed), the graph runs one more model step.
- That step gets a user-role note: "Your task '<title>' has changes that are not in a pull request. If the work is finished, open one with OpenPullRequest. Otherwise say what you are waiting for."
- It happens at most once per turn, and the note is not stored.
- This replaces "open a PR automatically when the agent finishes" (`project_knowledge.md`, Changed).

## 7. Agents talking to each other

**ListAgents()**, risk class `read`. It returns every agent:
- name, display name and description;
- whether it chats;
- its status from the status board (§8);
- its current task and open PRs;
- whether the caller may message it.

**SendMessage(to, message)**, risk class `message`:
- `to` must be in the sender's `can_message`.
- The message is stored in the recipient's session, as stage 1 already models it: `author_kind` `user`, `from_agent` the sender, and the sender's display name as the author name. It appears there labeled "From <agent>".
- The sender gets "Sent to <agent>." and does not wait.

**Delivery** (the lead's decision: a message reaches the recipient mid-turn):
- Recipient idle: the message starts a turn, like a user message.
- Recipient running: the message is delivered at the recipient's next model step, after the current step's tool results and never between a tool call and its result. The model node appends every undelivered inbound message before calling the model. A delivered message records the turn that read it.
- Recipient waiting: delivered at the first model step after it resumes.
- After every turn, an inbound message still undelivered starts the next turn. Nothing is lost when a message arrives just after the last model step.
- User messages keep stage 1's queue: one turn at a time, in order.

**Replies and exchanges:**
- A reply is a SendMessage back. ID prefix `xch` for an exchange.
- A message continues an exchange, with its hop count plus one, when the sender's current turn read a message from that recipient in that exchange. Otherwise it starts a new exchange at hop 1.
- Limits per project: at most 6 hops, and at most 300,000 tokens per exchange. The tokens are the `llm_calls` of the turns that read the exchange's messages.
- Past a limit, SendMessage returns a tool error telling the agent to report to the user instead.
- Policy still applies to the recipient's own actions: a message never raises anyone's permissions.

**ScheduleWakeup(delay_seconds, note)**, risk class `message`, for agents whose definition has `schedules: true`:
- It stores a wake-up (ID `wak`, at most 5 pending per agent, delay from 60 s to 7 days).
- When it is due, a system-labeled message "Scheduled wake-up: <note>" is delivered to the agent's own session, by the delivery rules above.
- One in-process timer waits for the next due wake-up. Wake-ups due during downtime fire at startup.
- The lead's decision: scheduled work runs in the agent's one session, labeled with its source.

## 8. Events, audit and the status board

**Events:**
- They follow the roadmap's record (ID `evt`) and are stored in `project.db`.
- One owner, `EventLog.record(...)`, stores an event and publishes it to the project's event hub.
- `GET /api/projects/{id}/events/stream` (SSE) sends them live, with the same reconnect rules as the session stream.
- The types are:
  - `turn.started`, `turn.waiting`, `turn.ended`;
  - `task.started`, `task.ended`;
  - `pr.opened`, `pr.updated`;
  - `issue.opened`, `issue.commented`;
  - `approval.requested`, `approval.decided`;
  - `message.sent`, `wakeup.scheduled`;
  - `sandbox.build.started`, `sandbox.build.finished`, `sandbox.build.failed`.

**Audit:**
- Records follow the roadmap's audit record (ID `aud`): every gate decision, every user decision on an approval, and every action of a `write_code`, `run` or `github` tool that ran.
- One owner, `AuditLog.record(...)`. Stage 6's Activity page reads them; stage 2 writes them.

**Status board:**
- One function derives each agent's state from the events and the live sessions:
  - `idle`, `running`, `waiting for approval` or `waiting for a reply`;
  - its current task;
  - its open PRs;
  - its last finished turn, as the first 200 characters of its final text.
- A definition with `status_board: true` (a new frontmatter field, default false; set for the Orchestrator) gets a compact summary in its system context at every turn.
- ListAgents reads the same function.

## 9. The seven agents

All are definition files on the one runtime.

| Agent | Chat | Tools | May message | Wake-ups |
|---|---|---|---|---|
| Orchestrator | yes | all general tools, the task tools, PR and issue tools, ListAgents, SendMessage | all | yes |
| Onboarding & Diagnosis | yes | same | Orchestrator, Data Quality, Model Performance | yes |
| Data Quality | yes | same | Orchestrator, Model Performance, Retraining, Alert Prioritization | yes |
| Model Performance | yes | same | Orchestrator, Data Quality, Retraining, Alert Prioritization | yes |
| Retraining | yes | same | Orchestrator, Model Performance, Deployment & Rollback | yes |
| Deployment & Rollback | yes | same | Orchestrator, Retraining, Model Performance | yes |
| Alert Prioritization | no | Read, Glob, Grep, ListAgents, SendMessage, SearchIssues, OpenIssue, CommentOnIssue | all | no |

"All general tools" means Read, Glob, Grep, Write, Edit and Bash. "The task tools" means StartTask and ResumeTask. The PR and issue tools are OpenPullRequest, SearchIssues, OpenIssue and CommentOnIssue.

Each prompt states:
- the agent's role, from `project_knowledge.md`;
- what it can do now, and nothing it cannot. Data connections, checks and deploy hooks come in later stages, and the prompts say so when asked.
- how to work: start a task before changing code, read before editing, test in the sandbox, open a PR when done, search issues before opening one, and message only when another agent's part is needed.

Alert Prioritization has no trigger until stage 4. It is listed with the status "starts in a later stage" and never runs in stage 2.

## 10. UI

**Agents page:**
- A list of the chat agents, each with its status and a badge for pending approvals. Selecting one opens its session.
- Messages from other agents are labeled "From <agent>".
- **Approval card:** inline in the agent's message, where the tool step would be. It shows the tool, the preview (a diff view for Write and Edit, the command for Bash, the PR or issue text), Approve, and Reject with an optional note. The turn shows "Waiting for your approval".
- **Task bar:** above the composer. It shows the current task's title, its branch, and a link to its PR. "Discard task" asks for confirmation first.
- A warning bar shows while the project is on `bypass`.

**Inbox page** (project level), whose sidebar entry shows the number of pending approvals:
- Approvals: pending, across agents, with the same cards.
- Changes to merge: local reviews (§6).
- Pull requests and issues opened by agents, with their state and links.

**Project settings** gains four sections:
- Permissions: the level, with the bypass warning.
- Sandbox: Docker status, the environment image's status, Rebuild, and the network toggle.
- GitHub issues: the minimum priority.
- Agent messages: the hop and token limits.

The project event stream keeps statuses and badges live.

## 11. Storage

- One project migration (`0002`) adds the tables `tasks`, `file_claims`, `approvals`, `events`, `audit`, `exchanges` and `wakeups`.
- It adds `exchange_id`, `hop` and `read_by_turn` to `messages`, and the message status `waiting` (SQLite's CHECK constraint means rebuilding the `messages` table in the migration).
- Project settings gain `permission_level`, `sandbox_network`, `issue_min_priority`, `message_max_hops` and `message_max_tokens`.
- The roadmap's ID table gains `tsk` (task), `xch` (exchange) and `wak` (wake-up), all in stage 2.

## 12. Error handling

- Every tool failure is a tool result the model reads, never the end of a turn. That covers Docker missing, a failed build, GitHub not linked, a claim conflict, a denylisted path, a denial and a rejection.
- Secrets never reach a container, an image, a commit, a log or an error. Git output and GitHub errors are scrubbed as in stage 1.
- A worktree or container found broken at turn start (deleted by hand, Docker restarted) is recreated from the task's branch. The agent is told in a system note.

## 13. Testing

- **Sandbox:** the real Docker tests run on Linux CI only (a `docker` marker). Every platform tests:
  - command building;
  - template selection;
  - the build-context contents (no repository files);
  - the Docker-missing path.
- **GitHub:** the GitHub tools are tested against a fake GitHub REST server, the way stage 1 tests providers against a fake. Real GitHub runs only in live tests.
- **Worktrees:** real repositories. Prove:
  - sparse exclusion, and that a denylisted tracked file is never committed as deleted;
  - a denylisted new file blocks the commit;
  - claim conflicts on Write, Edit and commit;
  - cleanup after a merge.
- **Approvals:**
  - pause and resume with the real checkpointer, including a resume after the session manager is rebuilt (a restart);
  - the gate re-running without duplicate approvals;
  - Stop on a waiting turn.
- **Messaging:**
  - delivery while idle, running and waiting;
  - never between a tool call and its result;
  - a message arriving after the last model step starts a turn;
  - the hop and token limits.
- **Policy:** the whole table, one test per cell.
- **The PR reminder:** it fires once, and not when there are no changes.
- **E2E:** the stage 1 flow plus one approval. On `ask`, the Orchestrator writes a file, the user approves in the chat, and the diff appears in the tool step.

## Done when

- On a GitHub project, at `ask`:
  - the user asks an agent for a change;
  - the agent starts a task, edits files and runs tests in the sandbox, with each risky action approved in the chat;
  - it opens a PR whose diff contains only that task's changes and no secret file.
- Two agents can message each other, mid-turn, within the limits, and the Orchestrator's status board shows what each is doing.
- A turn waiting for approval survives an app restart and resumes when approved.
- On a machine without Docker, everything except Bash works and Bash explains why.

## Open questions for the lead

1. **Local projects without a GitHub remote: what does approving a local review do?** It merges the task branch into the default branch. If the user has that branch checked out, merging changes their working files, which stage 1 promised never to do. The options:
   - (a) merge only when the default branch is not checked out, or is checked out and clean, and say so before approving;
   - (b) never touch the working tree: move the branch ref only when it can fast-forward, and otherwise tell the user to merge themselves;
   - (c) leave the agent branch for the user to merge, and approving only marks it reviewed.
