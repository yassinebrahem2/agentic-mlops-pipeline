# Stage 2: Agent runtime, full

Date: 2026-10-24. Status: approved by the lead on 2026-10-24.
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

It is built for every tool call, not once per turn, so a call after `StartTask` in the same turn sees the new worktree.

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
- Both have risk class `write_code`: they create branches and worktrees.
- `ResumeTask(task_id)` makes an earlier `active` or `in_review` task of the same agent current again, for example to answer review comments. An unknown id, or a task of another agent, is a tool error; nothing is substituted.
- Write, Edit or Bash with no current task returns a tool error: "Start a task first with StartTask."

**Creating the worktree:**
- The base is the remote's default branch, fetched first, for every project that has a GitHub link (§6): a GitHub project's managed clone, or a local project whose `origin` is on github.com. A PR then holds only the task's changes, never the user's unpushed commits. For a local project, the fetch writes one namespaced ref (§6) to the repository's git data, which this spec adds to the stage 1 promise; working files, the checked-out branch and the user's own refs are untouched. A local project without a GitHub remote bases on its local default branch's commit.
- The steps:
  1. `git worktree add -b <branch> <path> <base>`;
  2. `git update-index --skip-worktree` on every tracked file matching the secret denylist;
  3. delete those files from the worktree, before any tool or container can see it.

  Tracked secret files are then absent from the worktree, and a commit never records them as deleted. Sparse-checkout is not used: in a worktree it turns on `extensions.worktreeConfig` in the user's shared repository config. A test proves all of this on a real repository, including that the user's `.git/config` is unchanged.
- The denylist is the one in `agents/tools/files.py` (stage 1); every user of it imports it.
- For local projects this adds a branch and worktree metadata to the user's repository's git data, as the stage 1 spec announced. Their working files and checked-out branch are untouched.

**Host git on a worktree.** The sandbox can write anything in the worktree, its `.git` entry included. A planted `.git` with `core.fsmonitor` or hooks would make the next host git command run code on the host. So:
- every host git call on a task passes `GIT_DIR` and `GIT_WORK_TREE` from the task record, never trusting the worktree's `.git` entry;
- it also passes `-c core.hooksPath=<an empty folder in the data folder>` and `-c core.fsmonitor=false`. Agent commits run no hooks, the user's included.

It also passes `-c core.longpaths=true`, since task worktrees sit deep in the data folder on Windows, and worktrees are deleted with stage 1's read-only-aware removal. One function in `agents/tasks/` builds these arguments; no other code runs git on a task. A test rewrites `.git` from the sandbox, then commits, and proves the commit lands in the right repository and nothing ran.

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
- The user can discard a task from the task bar. That removes the worktree, container, branch and claims after a confirmation; an open PR is left alone. Discard is refused while that agent's turn is running or waiting; the user stops it first.

## 3. File tools

Claude Code's names and argument shapes:
- `Write(file_path, content)`
- `Edit(file_path, old_string, new_string, replace_all=false)`

Both have risk class `write_code`.

- Paths resolve inside the current task's worktree, with the stage 1 rules (no `..`, no symlink escapes). A denylisted name is refused.
- Edit, and Write over an existing file, need the file's content to match a hash recorded when the agent last Read it in this task. That also catches changes Bash or another tool made since. Otherwise the tool error is "Read <path> first" or "<path> changed since you read it". An approved Write or Edit checks again when it runs, so a file that changed while waiting for approval fails rather than being overwritten.
- Edit fails when `old_string` is missing, or appears more than once without `replace_all`, saying which.
- Line endings and encoding are kept: UTF-8, and a file's existing CRLF or LF. Files are written as bytes, so the platform never translates line endings.
- The final open does not follow symlinks (Bash can create them), and the resolved path is checked again at open time.
- Both tools compute their change as a unified diff first. That diff is the approval preview (§5) and the tool's success output, capped like Bash output.

## 4. Sandbox and Bash

**Docker:**
- Bash needs Docker. The app checks `docker version` at startup and on demand from project settings.
- Without Docker, Bash returns a tool error saying Docker is needed and how to get it, and every other tool works.

**The environment image, built from the repo's own files:**
- The runtime picks a template from the worktree's current dependency files, checked at every Bash call, in this order:
  1. `environment.yml` or `environment.yaml`: micromamba;
  2. `uv.lock` with `pyproject.toml`: `uv sync --frozen --no-install-project`;
  3. `pyproject.toml`: its `[project].dependencies` installed with pip;
  4. `requirements.txt`: `pip install -r`;
  5. none of these: the base image only.
- The Python version comes from `.python-version`, else the lower bound of `requires-python`, else 3.12.
- The build context holds only the template and the dependency files, never the repository, so no secret can enter an image.
- The build runs with network access, a 15-minute timeout and its own status (`sandbox.build.*` events, shown in the tool step and in project settings).
- The tag is `<app name>-env:<project id>-<12 hex of sha256(template + dependency files)>`, with the app name from its one constant. An unchanged environment is never rebuilt. When an agent changes a dependency file, the next Bash call builds the new image (with network) and recreates the task's container on it. When a new tag builds, the project's older tags no running container uses are removed.
- Installed environments live outside `/workspace` (for uv, `UV_PROJECT_ENVIRONMENT=/opt/venv`), so the mount never hides them.
- A failed build is a tool error carrying the last 50 lines of the build log. The Bash call that triggered it ends there; the next Bash call tries again.

**A container per task:**
- Started on the task's first Bash call with:
  - `--network none`, unless the project allows network;
  - `--cpus 2 --memory 4g --pids-limit 512`;
  - the task's worktree mounted read-write at `/workspace` (the working directory);
  - the host user's uid and gid on Linux and macOS;
  - labels with the project and task;
  - no host environment variables; `HOME=/tmp`, since the host uid has no home in the image.
- `PYTHONPATH=/workspace/src:/workspace`, so the project's own code imports without an install, which needs network.
- It is stopped and removed when its task ends and when the app shuts down. Containers left behind by a crash are removed at startup by label.

**Bash:**
- `Bash(command, timeout?, description?)`, risk class `run`.
- It runs `bash -lc <command>` in the task's container.
- The timeout defaults to 120 s, at most 600 s. A timeout restarts the container, which ends every process; the worktree is kept.
- The output is stdout and stderr combined, plus the exit code. A non-zero exit is a normal result, not a tool error. Capture is bounded while the command runs (the first and last 15,000 characters are kept), so a runaway command cannot exhaust memory, and the cut is noted.
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
- model → decide → wait → tools → model, and the end of a turn goes through the PR reminder (§6).
- Two nodes, because LangGraph re-runs a resumed node from its start. `decide` never pauses, so it runs once per step; `wait` pauses and holds no side effects, so re-running it is harmless.
- Arguments are validated against the tool's pydantic model before the gate, so a malformed call gets its tool error and never becomes an approval request. The same model produces the tool's schema.
- Tool calls of a step run one after another, never in parallel.
- `decide`: for each tool call of the step, it decides allow, deny or ask from the project's level, writes one audit record per decision, and returns the decisions in the graph state (checkpointed when the node completes). For the asks, it creates the approval requests and their message parts.
- For the asks, it creates approval requests: ID `apr`, keyed by the tool-call id, so a re-run finds them instead of duplicating them. Each carries the tool's preview:
  - the diff for Write and Edit;
  - the command for Bash;
  - the title, body and target for GitHub tools, and for OpenPullRequest also the staged diff that will be pushed, so changes made by Bash are shown before they leave the machine.
- `wait`: while any of the step's approvals is pending, it calls `interrupt()`. When resumed, it reads the decisions from the approval records, the one source; the audit log is never read back. A level change while a turn waits applies from that turn's next step.

**In the session.** An approval is a message part (the context builder and compaction skip it; the contracts task lists every reader of `Part`, frontend included), so the chat shows it on reload and live with no new mechanism:
`{"type": "approval", "approval_id", "tool_call_id", "preview", "status": "pending" | "approved" | "rejected" | "cancelled" | "expired", "note"}`, placed after its tool call in the agent's message and updated through the existing `message.updated` event. `SessionView` gains `current_task` (id, title, branch, PR number and URL, or null) for the task bar. These two changes, and the message status `waiting`, are additions to I1; the frontend types are regenerated from them.

**Waiting:**
- When the graph pauses, the executor marks the turn `waiting`, and the agent message `waiting` (a new message status). It publishes `approval.requested`, and keeps the turn's checkpoint thread (stage 1 deleted it at every turn end).
- The session's queue holds behind a waiting turn, as behind a running one.
- On startup, `waiting` turns stay as they are.

**Deciding and resuming:**
- The user approves or rejects each request, with an optional note, from the chat card or the Inbox. Each decision writes an audit record.
- When the last request of the step is decided, the session resumes the turn. The user's decisions are read from the approval records; the resume value is only a wake signal (`Command(resume=True)`), never a mapping LangGraph could read as interrupt ids. It uses a new checkpointer connection, so this also works after a restart.
- Resuming loads the waiting agent message's id and parts into the executor before the graph continues, so the message keeps everything it had before the pause, after a restart too.
- The tools step runs the approved calls. It answers each rejected call with the tool error "The user rejected this action" plus the note.

**Approvals whose turn is gone:**
- At startup, every pending approval whose turn is not `waiting` (it was `running` when the app stopped, and is now `interrupted`) becomes `expired`, and its message part says so.

**Stop on a waiting turn:**
- It marks the turn's pending approvals `cancelled` (not `rejected`, so the audit tells the user's rejection from a Stop).
- It ends the turn `stopped` and deletes its thread.

## 6. Pull requests, issues and the end-of-turn reminder

**GitHub link:**
- A project can use GitHub when it is a GitHub project, or a local project whose `origin` is a github.com repository, and the GitHub integration is set. Without a link, OpenIssue, CommentOnIssue and SearchIssues return a tool error saying so, and OpenPullRequest opens a local review.
- Pushing, PRs, labels and issues need the token to have contents, pull-requests and issues write access. A GitHub refusal for lack of permission becomes a tool error naming the missing permission, and the Integrations page lists the permissions needed.
- Tokens reach git only through stage 1's `GIT_ASKPASS` runner.

**Fetching and pushing** never go through the user's configured remote: if their `origin` is SSH, ssh could wait on a passphrase or host-key prompt. Every fetch and push uses `https://github.com/<owner>/<repo>.git`, built from the project's owner and repository, with the token through stage 1's runner, plus `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` as a backstop. A local project's base is fetched into the namespaced ref `refs/<app name>/base/<default branch>`, so the user's own `origin/*` refs are never touched.

**Committing** (one function, used by OpenPullRequest and local review):
1. Stage everything in the worktree (`git add -A`, which respects `.gitignore`).
2. If a staged path matches the denylist, unstage everything and fail, naming the paths. The agent must delete them. Nothing secret is committed or pushed.
3. If a staged path is claimed by another open task, unstage everything and fail, naming the agent.
4. Commit as the user: the name and email of the user's git config, else their GitHub login with GitHub's noreply address. The message is the PR title. Never an AI identity.

**OpenPullRequest(title, body, fixes=[])**, risk class `github`:
- With no PR yet: commit, push the branch, and open a PR into the default branch. The body ends with a line saying which agent opened it and from which task, plus `Fixes #n` for each entry of `fixes`. The task becomes `in_review`, and the event is `pr.opened`.
- With a PR already open: commit, push, and update the title and body if they changed (`pr.updated`).
- A local project without a GitHub remote gets a local review instead: an Inbox entry with the diff against the base. Rejecting closes the task, and a system message tells the agent. Approving merges it (the lead's decision), which narrows the stage 1 promise to "never without your explicit approval":
  1. The merge of the task branch into the default branch is built in a temporary worktree in the data folder. A conflict is reported to the user and the agent, and nothing in the user's repository changes.
  2. The only step in the user's repository is `git merge --ff-only <that merge commit>`. Git itself refuses when uncommitted changes are in the way, atomically, so there is no separate cleanliness check to race the user's editor. When the default branch is checked out in none of the repository's worktrees (`git worktree list`), only the branch moves.
  3. Both steps use the one task git function: no hooks, no fsmonitor.
  4. The approval screen lists the files in the user's working tree that the merge will change.

  A merged review ends the task (`merged`).

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
- "Waiting for a reply" on the status board means the agent sent the last message of an exchange that is still open (no limit reached, no reply yet).
- After every turn, an inbound message still undelivered starts the next turn. Nothing is lost when a message arrives just after the last model step.
- User messages keep stage 1's queue: one turn at a time. When a turn ends, the next turn starts from the earliest waiting input, queued user message or undelivered agent message, by arrival order (message `seq`).

**Replies and exchanges:**
- A reply is a SendMessage back. ID prefix `xch` for an exchange.
- A message continues an exchange, with its hop count plus one, when the sender's current turn read a message from that recipient in that exchange. Otherwise it starts a new exchange at hop 1.
- Limits per project: at most 6 hops, and at most 300,000 tokens per exchange. The tokens are the `llm_calls` of the turns that read the exchange's messages.
- Past a limit, SendMessage returns a tool error telling the agent to report to the user instead.
- Policy still applies to the recipient's own actions: a message never raises anyone's permissions.

**ScheduleWakeup(delay_seconds, note)**, risk class `message`, for agents whose definition has `schedules: true`:
- It stores a wake-up (ID `wak`, at most 5 pending per agent, delay from 60 s to 7 days). At most 24 wake-ups fire per agent per day; past that, ScheduleWakeup returns a tool error, so an agent cannot keep itself running alone.
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
- One owner, `AuditLog.record(...)`. Stored arguments are scrubbed of secrets and capped (4,000 characters per value). Stage 6's Activity page reads them; stage 2 writes them.

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
| Orchestrator | yes | all general tools, the task tools, PR and issue tools, ListAgents, SendMessage | all but Alert Prioritization | yes |
| Onboarding & Diagnosis | yes | same | Orchestrator, Data Quality, Model Performance | yes |
| Data Quality | yes | same | Orchestrator, Model Performance, Retraining | yes |
| Model Performance | yes | same | Orchestrator, Data Quality, Retraining | yes |
| Retraining | yes | same | Orchestrator, Model Performance, Deployment & Rollback | yes |
| Deployment & Rollback | yes | same | Orchestrator, Retraining, Model Performance | yes |
| Alert Prioritization | no | Read, Glob, Grep, ListAgents, SendMessage, SearchIssues, OpenIssue, CommentOnIssue | all chat agents | no |

"All general tools" means Read, Glob, Grep, Write, Edit and Bash. "The task tools" means StartTask and ResumeTask. The PR and issue tools are OpenPullRequest, SearchIssues, OpenIssue and CommentOnIssue.

Each prompt states:
- the agent's role, from `project_knowledge.md`;
- what it can do now, and nothing it cannot. Data connections, checks and deploy hooks come in later stages, and the prompts say so when asked.
- how to work: start a task before changing code, read before editing, test in the sandbox, open a PR when done, search issues before opening one, and message only when another agent's part is needed.

Alert Prioritization has no trigger until stage 4, and no agent may message it in stage 2 (stage 4 adds it to their `can_message`). It is listed with the status "starts in a later stage" and never runs in stage 2.

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

- Project migrations add the tables `tasks`, `file_claims`, `approvals`, `events`, `audit`, `exchanges` and `wakeups`.
- It adds `exchange_id`, `hop` and `read_by_turn` to `messages`, and the message status `waiting` (SQLite's CHECK constraint means rebuilding the `messages` table in the migration).
- Project settings gain `permission_level`, `sandbox_network`, `issue_min_priority`, `message_max_hops` and `message_max_tokens`.
- Each plan adds its own numbered migration; the stage 2 overview assigns the numbers, so plans never edit the same file.
- Removing a project also runs `git worktree prune` and deletes the `agent/*` branches in a local project's repository, after removing its containers.

## Changes to the roadmap

Made in `docs/roadmap.md` before any plan is issued:
- IDs: `tsk` (task), `xch` (exchange), `wak` (wake-up), stage 2.
- Risk classes: StartTask and ResumeTask are `write_code`; SearchIssues is `read`; ScheduleWakeup is `message`.
- The agent definition format gains `status_board: false`.
- The policy table's values (§5).
- The PR rule ("reminded at the end of each turn") and file claims enforced in code are already in the roadmap's Stage 2 text or replace it as this spec says.

## 12. Error handling

- Every tool failure is a tool result the model reads, never the end of a turn. That covers Docker missing, a failed build, GitHub not linked, a claim conflict, a denylisted path, a denial and a rejection.
- Secrets never reach a container, an image, a commit, a log or an error. Git output and GitHub errors are scrubbed as in stage 1.
- A worktree found broken at turn start (deleted by hand) is recreated from the task's branch, losing uncommitted changes. A container found gone (Docker restarted) is recreated on the same worktree, which keeps the files but not running processes. The agent is told in a system note either way.

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
  - pause and resume with the real checkpointer, including a resume after the session manager is rebuilt (a restart), with the earlier parts of the message kept;
  - `decide` running once per step: resuming creates no second approval or audit record;
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

## Build order

Everything in this spec ships in stage 2. Plans are ordered so the core lands first and the others build on it in parallel:
1. Contracts: `ToolContext` v2, the I1 additions, the event and audit records, the policy table, and the migration numbers.
2. In parallel: tasks and worktrees with Write and Edit; the sandbox with Bash; the gate and approvals; events, audit and the status board; GitHub PRs and issues; messaging and wake-ups.
3. Then: the seven definitions, the agents page, the Inbox and the settings sections.
