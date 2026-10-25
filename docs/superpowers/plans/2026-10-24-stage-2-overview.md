# Stage 2: Agent Runtime, Full — Plans Overview

Stage 2 is built through several plans. This overview holds what they share. Read it and `docs/roadmap.md` (Stage 2) before starting any plan.

**Goal:** All seven agents on the shared runtime, with Write/Edit, Bash in a Docker sandbox, per-task git worktrees, PRs and issues, agent-to-agent messaging, the permission policy with approvals, and the audit log and event stream. Alongside it: the fixes and UI changes from the lead's stage 1 walkthrough.

**Spec:** the runtime plans follow the stage 2 spec (`docs/superpowers/specs/2026-10-24-stage-2-agent-runtime-design.md`, approved). The walkthrough plans below need no spec: each implements behavior already agreed in `docs/roadmap.md` (Stage 2, the last two bullets of "Agreed"), with the causes found in the code.

**Tech stack:** as in stage 1 (`2026-10-16-stage-1-overview.md`).

## Global constraints

- Focus styling has one owner: the base rule in `frontend/src/index.css` (ui-polish task 1). No component adds its own `focus-visible:ring-*` or `outline-*` classes.
- Value pickers use the app's components: `components/ui/select.tsx` for a fixed list, `components/ui/combobox.tsx` (pickers task 1) for a list that can be filtered or typed into. No native `<select>`.
- Every user-facing message has one source. A message the server already sends (for example `ModelEntry.problem`) is shown as sent, never rewritten in the UI.
- Frontend tests run with nothing else heavy alongside (Decision #120).

## Plans

| Plan | Branch | Covers | Consumes (besides merged code) | Starts | State |
|---|---|---|---|---|---|
| tool-errors | `plan/stage-2-tool-errors` | Bug #144 (invalid tool-call arguments dropped), a provider's rejection of a tool call goes back to the model, clearer tool schemas | none | now | Merged |
| models | `plan/stage-2-models` | Context window read from Gemini and vLLM; model warnings above the models table, rows of one height | none | now | Merged |
| ui-polish | `plan/stage-2-ui-polish` | Pointer cursor, one focus style, styled selects in settings, compact provider cards, bordered markdown tables | none | now | Merged |
| pickers | `plan/stage-2-pickers` | One combobox component; the GitHub repo picker and the model picker use it | none | now | Merged |
| chat-layout | `plan/stage-2-chat-layout` | Only the message list scrolls; a one-line composer that grows; one Send/Stop button; the styled agent switcher | none | now | Merged |
| project-context | `plan/stage-2-project-context` | App pages keep the current project; the sidebar header layout | none | now | Merged |
| fixes | `plan/stage-2-fixes` | Review bugs #216, #217 (composer Stop with whitespace, chat layout leftovers) and #226 (one GitHub https URL builder) | none | now | Merged |
| runtime-fixes | `plan/stage-2-runtime-fixes` | Review bugs #242 (a turn's end can be lost) and #236 (provider key values read twice) | none | now | Planned |
| runtime-contracts | `plan/stage-2-runtime-contracts` | I8 to I12 in code, the whole stage 2 schema, the graph's hook points with stage 1 defaults | none; task 4 after tool-errors merges | now | Merged |
| events | `plan/stage-2-events` | Event log, project event stream, audit log, status board, the turn observer | I8 to I12 | after runtime-contracts tasks 1 and 2 (task 3 for the observer); task 3: runtime-contracts task 4 merged | Merged |
| tasks | `plan/stage-2-tasks` | Tasks, worktrees, the one task git function, file views, StartTask and ResumeTask, Write and Edit, claims, committing, local merge, discard | I8 to I12; events task 1 | tasks 1 to 4: after runtime-contracts tasks 1 to 3 and events task 1; task 5 (wiring): runtime-contracts task 4 merged | Planned |
| sandbox | `plan/stage-2-sandbox` | Docker detection, environment images, task containers, Bash, sandbox settings and routes | I8 to I12; events task 1 | after runtime-contracts task 1 | Planned |
| approvals | `plan/stage-2-approvals` | The gate, approval records and parts, waiting and resuming turns, expiry, Stop, approvals and permission-level routes | I8 to I12; events task 1 | tasks 1 and 2: after runtime-contracts tasks 1 and 2 and events task 1; task 3 on: runtime-contracts task 4 merged | Planned |
| github-work | `plan/stage-2-github-work` | GitHub client for PRs, issues and labels, the fake GitHub server, OpenPullRequest, issue tools, local reviews, the PR reminder, PR state sync | I8 to I12; tasks task 4; events task 1 | client: after runtime-contracts task 1; tools: after runtime-contracts task 3 and the tasks plan's commit task; task 5: runtime-contracts task 4 merged; tasks 4 and 5: the tasks plan merged | Planned |
| messaging | `plan/stage-2-messaging` | SendMessage, exchanges and limits, mid-turn delivery, ListAgents, ScheduleWakeup and its timer, messaging settings | I8 to I12; events tasks 1 and 3 | after runtime-contracts tasks 1 to 3 and events task 1 (task 4 merged before its task 2) | Planned |
| agent-definitions | `plan/stage-2-agent-definitions` | The seven definition files and their prompts | every stage 2 tool on `main` | after tasks, sandbox, github-work and messaging | Planned |
| frontend-agents | `plan/stage-2-frontend-agents` | Agents list with status, approval cards, the task bar, the bypass warning, "From <agent>" labels | I9, I11, the approvals, tasks and events routes | coding: after runtime-contracts task 1 and chat-layout merged; merging: after the routes it uses | Planned |
| frontend-inbox | `plan/stage-2-frontend-inbox` | The Inbox page and its badge, project settings for permissions, sandbox, issues and messaging | I9, I11, the routes it uses; frontend-agents tasks 1 and 2 (`useProjectEvents`, `ApprovalCard`) | coding: after runtime-contracts task 1, project-context merged and frontend-agents tasks 1 and 2; merging: after the routes it uses | Planned |

## Files shared by these plans

Each file below is edited by one plan only:
- `index.css`, the `components/ui/*` ring classes, `ProviderCard.tsx`, `AgentDefaults.tsx`, `AgentsSection.tsx`, `MessageView.tsx`: ui-polish. runtime-contracts task 1 adds one line to `MessageView.tsx` (Decision #218).
- `components/ui/combobox.tsx`, `GitHubTab.tsx`, `AddModelDialog.tsx`: pickers.
- `AppShell.tsx`, `AgentsPage.tsx`, `MessageList.tsx`, `Composer.tsx`: chat-layout.
- `Sidebar.tsx`, `ProjectSwitcher.tsx`, `ProjectsPage.tsx`, new `lib/useCurrentProject.ts` and `lib/useProjects.ts`: project-context. It also changes the one `invalidateQueries` line for the projects key in `useOpenProject.ts`, `LocalFolderTab.tsx`, `GitHubTab.tsx` (a pickers file), `RemoveProjectDialog.tsx` and `RepositorySection.tsx`.
- `ModelsTable.tsx`, `ProvidersPage.tsx`, `providers/service.py`, new `providers/listing.py`: models.
- `agents/runtime/graph.py`, `model_caller.py`, `providers/errors.py`, `agents/tools/base.py`, `agents/tools/grep.py`, `tests/fakes/fake_openai.py`: tool-errors.

Two test files are shared, and each plan edits only its own steps in them:
- `frontend/e2e/flow.spec.ts`: pickers (the Add model steps), ui-polish (the Orchestrator default-model step), models (the verified model row's cells).
- `frontend/src/pages/providers/ProvidersPage.test.tsx`: ui-polish (cards, default models) and models (the table).
- `frontend/src/pages/providers/ModelsTable.test.tsx`: models (the table) and pickers (its Add model dialog test).

The e2e flow relies on these names, which every plan keeps: the app-name heading, the "AI providers", "Projects" and "Agents" links, the `log` named "Messages", the "Message Orchestrator" input and the "Send" button.

The second plan to merge brings `main` in first. Every frontend plan runs `npm run build && npm run e2e` before its PR, and E2E CI must be green.

A plan that needs a change in another plan's file opens a Decision issue.

## Runtime sequencing

- `graph.py`, `executor.py` and `model_caller.py`: tool-errors edits them first, then runtime-contracts task 4 adds every hook point once. After that the runtime plans fill only their own modules. The exception is approvals, which owns the executor and session-manager changes for waiting and resuming.
- `api/services.py`: runtime-contracts sets the wiring up. Each plan changes only the line that builds its own service or default.
- `agents/tools/__init__.py` (`default_registry`): each plan adds only its own tools' lines.
- `frontend/src/pages/agents/*`: frontend-agents starts after chat-layout merges. Project settings and the sidebar badge: frontend-inbox starts after project-context merges.
- The only stage 2 migration is runtime-contracts task 2's `0002_stage2.sql`.
- `sessions.py` and `store.py` are at the size limit: runtime-contracts task 3 splits them first (`runtime/worker.py`, a `runtime/store/` package). Later plans add their own modules next to them and grow neither.

## Interfaces between plans

The walkthrough plans produce no interface another plan consumes. The runtime interfaces follow. runtime-contracts writes them into code exactly as written here, and changing one takes a Decision issue.

### I8: tool context and tool base (`agents/tools/base.py`)

```python
TaskStatus = Literal["active", "in_review", "merged", "closed", "discarded"]

@dataclass(frozen=True)
class TaskRef:
    id: str
    title: str
    branch: str
    worktree: Path
    status: TaskStatus

@dataclass(frozen=True)
class ToolContext:          # built for every tool call
    project_id: str
    agent: str
    session_id: str
    turn_id: str
    files: FileAccess        # the current task's worktree view, else the repository's
    task: TaskRef | None

class FileAccess(Protocol):   # stage 1 members unchanged, plus:
    def invalidate(self) -> None: ...   # drop the cached listing after a change

class Preview(BaseModel):
    kind: Literal["diff", "command", "text"]
    title: str
    body: str

class AgentTool[A: BaseModel](ABC):          # stage 1 members unchanged, plus:
    def parse(self, raw_args: Mapping[str, Any]) -> A | ToolOutput   # ToolOutput = the argument error
    async def preview(self, ctx: ToolContext, args: A) -> Preview     # default: kind "text", JSON args
```

A tool that needs a service receives it in its constructor; `default_registry(...)` takes those services.

### I9: session contract additions (`agents/runtime/contract.py`)

```python
MessageStatus = Literal["complete", "streaming", "queued", "stopped", "interrupted", "waiting"]
ApprovalStatus = Literal["pending", "approved", "rejected", "cancelled", "expired"]

class ApprovalPart(BaseModel):
    type: Literal["approval"]
    approval_id: str
    tool_call_id: str
    preview: Preview
    status: ApprovalStatus
    note: str | None

Part = Annotated[TextPart | ToolCallPart | ToolResultPart | ApprovalPart, Field(discriminator="type")]

class TaskView(BaseModel):
    id: str
    agent: str               # added in the review of #246: the status board groups tasks by agent
    title: str
    branch: str
    status: TaskStatus
    review_kind: Literal["github", "local"] | None
    pr_number: int | None
    pr_url: str | None

class SessionView(BaseModel):     # stage 1 fields, plus:
    current_task: TaskView | None

class TurnWaitingData(BaseModel):
    turn_id: str

class TurnWaiting(_Event):
    type: Literal["turn.waiting"]
    data: TurnWaitingData
# StreamEvent gains TurnWaiting. An approval part changes through the existing message.updated.

class AgentSessions(Protocol):    # stage 1 members, plus:
    async def post(self, project_id: str, agent: str, *, author_name: str, from_agent: str,
                   text: str, exchange_id: str | None, hop: int | None) -> MessageView: ...
    # An inbound message from another agent, or a wake-up (author_name "Scheduled wake-up",
    # from_agent = the agent itself). Stored as author_kind "user" with a queued turn, like send.
    async def turn_states(self, project_id: str) -> dict[str, Literal["running", "waiting"]]: ...
    # Added in the review of #246: each agent whose session has a running or waiting turn now,
    # read from the live sessions and the turns table (the status board's one source for both).
    async def update_message(self, project_id: str, session_id: str, message_id: str,
                             transform: Callable[[list[Part]], list[Part]]) -> MessageView: ...
    # Reads the stored parts, applies transform, stores and publishes message.updated, all under
    # the session lock, so two concurrent updates (two approvals decided at once) both land.
```

### I10: policy (`agents/policy/table.py`)

```python
PermissionLevel = Literal["suggest", "ask", "autonomous", "bypass"]
PolicyDecision = Literal["allow", "ask", "deny"]

def decide(risk: RiskClass, level: PermissionLevel) -> PolicyDecision   # the spec's §5 table, the one source
```

### I11: events, audit, status (`events/contract.py`)

```python
EventType = Literal[
    "turn.started", "turn.waiting", "turn.ended", "task.started", "task.ended",
    "pr.opened", "pr.updated", "issue.opened", "issue.commented",
    "approval.requested", "approval.decided", "message.sent", "wakeup.scheduled",
    "sandbox.build.started", "sandbox.build.finished", "sandbox.build.failed",
]
# Payload of each type (added in the review of #246). The writer's plan writes exactly these keys,
# `source` is the agent that acted ("user" or "system" otherwise), and readers use only these.
#   turn.started, turn.waiting:   {"turn_id"}
#   turn.ended:                   {"turn_id", "status", "final_text"}   final_text capped at 200
#   task.started:                 {"task_id", "title", "branch"}         subject_id = task_id
#   task.ended:                   {"task_id", "status"}                  subject_id = task_id
#   pr.opened, pr.updated:        {"task_id", "number", "url", "title"}  subject_id = task_id
#   issue.opened, issue.commented: {"number", "url", "title"}
#   approval.requested:           {"approval_id", "turn_id", "tool"}     subject_id = approval_id
#   approval.decided:             {"approval_id", "status"}              subject_id = approval_id
#   message.sent:                 {"to", "exchange_id", "hop"}           subject_id = exchange_id
#   wakeup.scheduled:             {"wakeup_id", "due_at"}
#   sandbox.build.*:              {"tag", "detail"}                      detail null unless failed

class EventRecord(BaseModel):
    id: str
    seq: int
    ts: str
    source: str              # agent name, "user" or "system"
    type: EventType
    subject_id: str | None
    payload: dict[str, Any]

AuditDecision = Literal["allowed", "asked", "denied", "approved", "rejected", "cancelled", "expired"]

class AuditRecord(BaseModel):
    id: str
    ts: str
    actor: str
    action: str              # the tool name, or "approval"
    risk_class: RiskClass
    permission_level: PermissionLevel
    decision: AuditDecision
    target: str
    detail: dict[str, Any]   # scrubbed, each value capped at 4,000 characters

AgentState = Literal["idle", "running", "waiting_approval", "waiting_reply", "inactive"]

class PrRef(BaseModel):
    number: int
    url: str
    title: str

class AgentStatus(BaseModel):
    agent: str
    display_name: str
    state: AgentState
    task: TaskView | None
    open_prs: list[PrRef]
    last_finished: str | None

class EventLog(Protocol):
    async def record(self, project_id: str, *, source: str, type: EventType,
                     payload: dict[str, Any], subject_id: str | None = None) -> EventRecord: ...
    def subscribe(self, project_id: str, after_seq: int | None) -> AsyncGenerator[EventRecord | Resync, None]: ...
    async def history(self, project_id: str, *, types: Sequence[EventType],
                      limit: int = 200) -> list[EventRecord]: ...   # newest first

class AuditLog(Protocol):
    async def record(self, project_id: str, *, actor: str, action: str, risk_class: RiskClass,
                     permission_level: PermissionLevel, decision: AuditDecision, target: str,
                     detail: dict[str, Any]) -> AuditRecord: ...

class StatusBoard(Protocol):
    async def snapshot(self, project_id: str) -> list[AgentStatus]: ...
    async def summary(self, project_id: str) -> str: ...   # the compact text a status_board agent gets
```

### I12: tasks, sandbox, approvals, messaging and the turn hooks

`agents/tasks/contract.py`:

```python
class ClaimConflict(Exception):     # its message names the other agent and task
    agent: str
    task_id: str

class CommitRefused(Exception): ...  # its message is written for the model

@dataclass(frozen=True)
class CommitResult:
    sha: str
    changed: tuple[str, ...]

class TaskWorkspaces(Protocol):
    async def current(self, project_id: str, session_id: str) -> TaskRef | None: ...
    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess: ...

class Tasks(TaskWorkspaces, Protocol):
    async def start(self, project_id: str, session_id: str, agent: str, title: str) -> TaskRef: ...
    async def resume(self, project_id: str, session_id: str, agent: str, task_id: str) -> TaskRef: ...
    async def view(self, project_id: str, task_id: str) -> TaskView: ...
    async def open_tasks(self, project_id: str) -> list[TaskView]: ...
    async def record_read(self, project_id: str, task: TaskRef, path: str, sha256: str) -> None: ...
    async def read_hash(self, project_id: str, task: TaskRef, path: str) -> str | None: ...
    async def claim(self, project_id: str, task: TaskRef, paths: Sequence[str]) -> None: ...  # raises ClaimConflict
    async def has_unpublished_changes(self, project_id: str, task: TaskRef) -> bool: ...
    async def staged_diff(self, project_id: str, task: TaskRef) -> str: ...  # what commit would record, capped
    async def commit(self, project_id: str, task: TaskRef, message: str) -> CommitResult: ...  # raises CommitRefused
    async def push(self, project_id: str, task: TaskRef) -> None: ...
    async def local_merge_preview(self, project_id: str, task: TaskRef) -> list[str]: ...  # user files that change
    async def local_merge(self, project_id: str, task: TaskRef) -> None: ...  # spec §6, steps 1 to 3; raises CommitRefused
    async def set_review(self, project_id: str, task_id: str, *, kind: Literal["github", "local"],
                         pr_number: int | None = None, pr_url: str | None = None) -> TaskView: ...
    async def end(self, project_id: str, task_id: str,
                  status: Literal["merged", "closed", "discarded"]) -> None: ...
```

`sandbox/contract.py`:

```python
class SandboxUnavailable(Exception): ...   # its message is written for the model and the user

class DockerStatus(BaseModel):
    available: bool
    version: str | None
    detail: str | None

class EnvironmentStatus(BaseModel):
    state: Literal["none", "building", "ready", "failed"]
    tag: str | None
    detail: str | None

class CommandResult(BaseModel):
    output: str
    exit_code: int | None    # None when timed out
    timed_out: bool
    truncated: bool

class Sandbox(Protocol):
    async def docker(self) -> DockerStatus: ...
    async def environment(self, project_id: str) -> EnvironmentStatus: ...
    async def rebuild(self, project_id: str) -> EnvironmentStatus: ...
    async def run(self, project_id: str, task: TaskRef, command: str, timeout_s: int) -> CommandResult: ...  # raises SandboxUnavailable
    async def remove_task(self, project_id: str, task_id: str) -> None: ...
    def paused(self, project_id: str, task_id: str) -> AbstractAsyncContextManager[None]: ...
    # Added in the review of #241: freezes the task's container (docker pause, then unpause on exit;
    # a no-op without a running container), so nothing in the sandbox can swap a file for a symlink
    # while the host writes or commits the worktree. Write, Edit, commit and staged_diff run inside it.
    async def close(self) -> None: ...
```

`agents/approvals/contract.py`:

```python
class ApprovalView(BaseModel):
    id: str
    agent: str
    session_id: str
    turn_id: str
    tool_call_id: str
    tool: str
    risk_class: RiskClass
    preview: Preview
    status: ApprovalStatus
    note: str | None
    created_at: str
    decided_at: str | None

class Approvals(Protocol):
    async def pending(self, project_id: str) -> list[ApprovalView]: ...
    async def decide(self, project_id: str, approval_id: str, *, approve: bool, note: str | None) -> ApprovalView: ...
```

`agents/messaging/contract.py`:

```python
class MessageLimitReached(Exception): ...   # its message is written for the model

class MessageBus(Protocol):
    async def send(self, project_id: str, *, sender: str, sender_turn_id: str, to: str, text: str) -> None: ...
    # SendMessage's whole behavior: can_message, exchange and hop, limits, post, event.
    def bind(self, sessions: AgentSessions) -> None: ...
    # The session manager binds itself once, in its constructor; a second call raises RuntimeError.
```

`agents/runtime/hooks.py` (new; the graph and the session manager call these):

```python
@dataclass(frozen=True)
class GateCall:
    tool_call_id: str
    message_id: str          # the agent message the approval part goes into
    tool: AgentTool[Any]
    args: BaseModel
    ctx: ToolContext

@dataclass(frozen=True)
class GateDecision:
    outcome: Literal["allow", "deny", "ask", "approved", "rejected"]
    message: str | None          # the tool error for deny and rejected
    approval_id: str | None      # set for an ask
    preview: Preview | None      # set for an ask

class Gate(Protocol):
    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]: ...   # never pauses; one decision for every call
    async def wait(self, project_id: str, turn_id: str,
                   decisions: dict[str, GateDecision]) -> dict[str, GateDecision]: ...  # interrupt() while pending

class InboundSource(Protocol):
    async def take(self, project_id: str, session_id: str, turn_id: str) -> list[BaseMessage]: ...

class TurnEndCheck(Protocol):
    async def note(self, project_id: str, session_id: str, turn_id: str) -> str | None: ...
    # The step a note causes is exempt from max_steps: at most one per turn.

class TurnObserver(Protocol):
    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None: ...
    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None: ...
    async def turn_ended(self, project_id: str, agent: str, turn_id: str, status: TurnEnd,
                         final_text: str | None) -> None: ...
```

Each default below lives in `hooks.py` and keeps stage 1's behavior until the plan in brackets wires the real hook and deletes it (Decision #234):
- `AllowAllGate` (approvals);
- `NoInbound` (messaging);
- `NoTurnEndCheck` (github-work);
- `RepositoryOnlyWorkspaces` (tasks).

The session manager takes a sequence of `TurnObserver`s, empty by default; events and github-work each add one.

The bus and the session manager need each other. `MessageBus` is built first, and the session manager binds itself to it once, in its constructor (`bus.bind(sessions)`). A second bind raises.

### HTTP routes the frontend plans use

All are under `/api/projects/{project_id}`.

| Route | Body or answer | Plan |
|---|---|---|
| `GET /events/stream` | SSE of `EventRecord`; `Last-Event-ID` resumes; `Resync` when the position is unknown | events |
| `GET /agents/status` | `list[AgentStatus]` | events |
| `GET /approvals` | pending `list[ApprovalView]` | approvals |
| `POST /approvals/{approval_id}/decision` | `{approve, note}` → `ApprovalView` | approvals |
| `GET`, `PUT /settings/permissions` | `{level: PermissionLevel}` | approvals |
| `POST /tasks/{task_id}/discard` | → 204 | tasks |
| `GET /reviews` | `list[LocalReview]`, each `{task: TaskView, agent, files_changed: list[str], diff}` | github-work |
| `POST /reviews/{task_id}/decision` | `{approve, note}` → `TaskView` | github-work |
| `GET /github/activity` | `list[GitHubItem]`, each `{kind: "pr" or "issue", number, title, url, state, agent, created_at}` | github-work |
| `GET`, `PUT /settings/issues` | `{min_priority: "low", "medium", "high" or "critical"}` | github-work |
| `GET /sandbox` | `{docker: DockerStatus, environment: EnvironmentStatus, network: bool}` | sandbox |
| `PUT /settings/sandbox` | `{network: bool}` | sandbox |
| `POST /sandbox/rebuild` | → `EnvironmentStatus` | sandbox |
| `GET`, `PUT /settings/messaging` | `{max_hops: int, max_tokens: int}` | messaging |
