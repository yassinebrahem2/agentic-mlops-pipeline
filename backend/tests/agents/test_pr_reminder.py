from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.pr_reminder import PrReminder
from mlops_agents.agents.runtime.graph import TurnHooks, TurnState, create_turn_graph
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelReply
from mlops_agents.agents.runtime.stall import LateApprovals
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.graph_env import RecordingSink, make_agent
from tests.agents.runtime.hook_fakes import NoInbound, RecordingCaller, ScriptedGate
from tests.agents.tasks.helpers import create_session, setup_service
from tests.conftest import MemoryKeyStore


async def test_pr_reminder_no_current_task(
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    paths = DataPaths(tmp_path / "home")
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    tasks, _, _, project_id, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_1"
    await create_session(databases, project_id, session_id, agent="coder")

    reminder = PrReminder(tasks)
    note = await reminder.note(project_id, session_id, "trn_1")
    assert note is None


async def test_pr_reminder_no_unpublished_changes(
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    paths = DataPaths(tmp_path / "home")
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    tasks, _, _, project_id, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_1"
    await create_session(databases, project_id, session_id, agent="coder")

    await tasks.start(project_id, session_id, "coder", "Add Feature")

    reminder = PrReminder(tasks)
    note = await reminder.note(project_id, session_id, "trn_1")
    assert note is None


async def test_pr_reminder_with_unpublished_changes(
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    paths = DataPaths(tmp_path / "home")
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    tasks, _, _, project_id, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_1"
    await create_session(databases, project_id, session_id, agent="coder")

    task = await tasks.start(project_id, session_id, "coder", "Add Feature")
    (task.worktree / "new_file.py").write_text("print('hello')\n", encoding="utf-8")

    reminder = PrReminder(tasks)
    note = await reminder.note(project_id, session_id, "trn_1")
    expected = (
        "Your task 'Add Feature' has changes that are not in a pull request. "
        "If the work is finished, open one with OpenPullRequest. Otherwise say what you are waiting for."
    )
    assert note == expected


async def test_pr_reminder_clean_after_changes_committed(
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    paths = DataPaths(tmp_path / "home")
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    tasks, _, _, project_id, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_1"
    await create_session(databases, project_id, session_id, agent="coder")

    task = await tasks.start(project_id, session_id, "coder", "Add Feature")
    (task.worktree / "new_file.py").write_text("print('hello')\n", encoding="utf-8")

    await tasks.commit(project_id, task, "feat: add feature")
    await tasks.set_review(project_id, task.id, kind="local")

    reminder = PrReminder(tasks)
    note = await reminder.note(project_id, session_id, "trn_1")
    assert note is None


async def test_turn_graph_with_pr_reminder_adds_step_when_changes_exist(
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    paths = DataPaths(tmp_path / "home")
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    tasks, _, _, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    store = SessionStore(databases)
    session_id = await store.session_id(project_id, "orchestrator")

    task = await tasks.start(project_id, session_id, "orchestrator", "Fix Issue")
    (task.worktree / "fixed.txt").write_text("fixed\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    agent = make_agent(repo, bin_dir, project_id=project_id)

    user_msg = await store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[],
        status="complete",
    )
    turn_id = await store.add_turn(project_id, session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        project_id,
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    caller = RecordingCaller(
        [
            ModelReply(
                message=AIMessage(content="I worked on the issue."),
                prompt_tokens=10,
                completion_tokens=5,
            ),
            ModelReply(
                message=AIMessage(content="I will open a PR now."),
                prompt_tokens=15,
                completion_tokens=6,
            ),
        ]
    )
    sink = RecordingSink()
    reminder = PrReminder(tasks)

    hooks = TurnHooks(
        gate=ScriptedGate(),
        inbound=NoInbound(),
        turn_end=reminder,
        workspaces=tasks,
        approval_store=ApprovalStore(databases),
        approvals=LateApprovals(),  # unbound: the recording caller never runs out of keys
        events=None,
    )
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id=project_id,
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=[],
        lock=AsyncRLock(),
        hooks=hooks,
    )
    initial: TurnState = {
        "messages": [HumanMessage(content="Please fix issue")],
        "step_count": 0,
        "stopped_reason": None,
    }
    await graph.ainvoke(initial, config={"configurable": {"thread_id": turn_id}})  # pyright: ignore[reportUnknownMemberType]

    # Model was called twice: once for initial answer, second for reminder
    assert len(caller.requests) == 2
    # The second request contains the reminder text from PrReminder
    last_msg = caller.requests[1][-1]
    assert isinstance(last_msg, HumanMessage)
    assert "Your task 'Fix Issue' has changes that are not in a pull request" in str(
        last_msg.content
    )
