from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

import httpx2
import pytest

from mlops_agents.agents.pr_sync import PrChange
from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.pulls import create_pull
from mlops_agents.knowledge.contract import Knowledge, NoteDraft, NoteType, NoteView, SearchResult
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.promotion.pr_closed import ClosedUnmergedObserver
from mlops_agents.workspace.refs import RepoRef
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github
from tests.fakes.fake_tasks import FakeTasks


@pytest.fixture
def fake_github() -> Iterator[RunningFakeGitHub]:
    state = FakeGitHubState()
    running = start_fake_github(state)
    original_api = api.GITHUB_API
    api.GITHUB_API = running.base_url
    try:
        yield running
    finally:
        api.GITHUB_API = original_api
        running.stop()


class FakeGitHubAuth:
    def __init__(self, value: str = "test") -> None:
        self.value = value

    async def token(self) -> str | None:
        return self.value


class FakeKnowledge:
    def __init__(self) -> None:
        self.notes: dict[str, NoteView] = {}
        self.save_calls: list[tuple[str, str, NoteDraft, str | None]] = []

    async def get(self, project_id: str, note_id: str) -> NoteView | None:
        return self.notes.get(note_id)

    async def save(
        self,
        project_id: str,
        author: str,
        note: NoteDraft,
        note_id: str | None = None,
    ) -> NoteView:
        self.save_calls.append((project_id, author, note, note_id))
        if note_id is not None:
            if note_id not in self.notes:
                raise DomainError("note_not_found", f"Note {note_id} not found", status=404)
            nid = note_id
            created = self.notes[note_id].created
        else:
            nid = new_id("kno")
            created = datetime.now(UTC)

        view = NoteView(
            id=nid,
            type=note.type,
            title=note.title,
            text=note.text,
            source=note.source,
            author=author,
            created=created,
            updated=datetime.now(UTC),
        )
        self.notes[nid] = view
        return view

    async def search(
        self,
        project_id: str,
        query: str,
        type: NoteType | None = None,
        limit: int = 10,
    ) -> SearchResult:
        query_terms = [t.lower() for t in query.split() if t.strip()]
        matched: list[NoteView] = []
        for n in self.notes.values():
            if type is not None and n.type != type:
                continue
            title_lower = n.title.lower()
            text_lower = n.text.lower()
            if not query_terms or all(
                term in title_lower or term in text_lower for term in query_terms
            ):
                matched.append(n)
        return SearchResult(notes=matched[:limit], used_vectors=False, detail=None)


class TasksWithRepo(FakeTasks):
    def __init__(self, owner: str = "test-owner", repo: str = "test-repo") -> None:
        super().__init__()
        self._ref = RepoRef(owner, repo)

    async def repo_ref(self, project_id: str) -> RepoRef | None:
        return self._ref


def _make_observer(
    *,
    knowledge: FakeKnowledge | None = None,
    http: httpx2.AsyncClient,
    github: FakeGitHubAuth | None = None,
    tasks: TasksWithRepo | None = None,
) -> ClosedUnmergedObserver:
    return ClosedUnmergedObserver(
        knowledge=cast(Knowledge, knowledge or FakeKnowledge()),
        http=http,
        github=cast(GitHubAuth, github or FakeGitHubAuth()),
        tasks=cast(Tasks, tasks or TasksWithRepo()),
    )


async def test_closed_unmerged_pr_writes_one_note_and_twice_seen_is_idempotent(
    fake_github: RunningFakeGitHub,
) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="Abandoned experiment",
            head="exp-branch",
            base="main",
            body="Initial body",
        )
        fake_github.state.comments[(repo, pr.number)] = [
            {"id": 101, "body": "Closing because baseline accuracy dropped.", "html_url": "url"}
        ]

        knowledge = FakeKnowledge()
        observer = _make_observer(
            knowledge=knowledge,
            http=http,
        )

        change = PrChange(
            number=pr.number,
            state="closed",
            merged_at=None,
            head_branch="exp-branch",
            agent="coder",
            files=["model.py"],
        )

        # First notification: note is saved
        await observer.changed("proj_1", change)
        assert len(knowledge.notes) == 1
        note = next(iter(knowledge.notes.values()))
        assert note.type == "decision"
        assert note.author == "system"
        assert (
            note.title
            == f"PR #{pr.number} closed unmerged: {untrusted('Abandoned experiment', 'github')}"
        )
        assert note.text == untrusted(
            "Closing because baseline accuracy dropped.", "github_comment"
        )

        # Second notification (in-memory cache): does not write a second note
        await observer.changed("proj_1", change)
        assert len(knowledge.notes) == 1
        assert len(knowledge.save_calls) == 1

        # Fresh observer to simulate restart / cleared in-memory cache: search finds existing note
        observer_restarted = _make_observer(
            knowledge=knowledge,
            http=http,
        )
        await observer_restarted.changed("proj_1", change)
        assert len(knowledge.notes) == 1
        assert len(knowledge.save_calls) == 1


async def test_fake_knowledge_save_raises_404_for_unknown_note_id() -> None:
    knowledge = FakeKnowledge()
    draft = NoteDraft(type="decision", title="Title", text="Text")
    with pytest.raises(DomainError) as exc_info:
        await knowledge.save("proj_1", author="system", note=draft, note_id="kno_unknown")
    assert exc_info.value.status == 404
    assert exc_info.value.code == "note_not_found"


async def test_closed_unmerged_pr_long_title_capped(
    fake_github: RunningFakeGitHub,
) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        long_title = "A" * 300
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title=long_title,
            head="exp-branch",
            base="main",
            body="Body",
        )
        knowledge = FakeKnowledge()
        observer = _make_observer(
            knowledge=knowledge,
            http=http,
        )
        change = PrChange(
            number=pr.number,
            state="closed",
            merged_at=None,
            head_branch="exp-branch",
            agent="coder",
            files=["model.py"],
        )
        await observer.changed("proj_1", change)
        assert len(knowledge.notes) == 1
        note = next(iter(knowledge.notes.values()))
        assert len(note.title) <= 200
        assert note.title.startswith(f"PR #{pr.number} closed unmerged:")


async def test_merged_and_open_prs_write_no_notes(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        knowledge = FakeKnowledge()
        observer = _make_observer(
            knowledge=knowledge,
            http=http,
        )

        merged_change = PrChange(
            number=1,
            state="merged",
            merged_at=datetime.now(UTC),
            head_branch="feature",
            agent="coder",
            files=["model.py"],
        )
        await observer.changed("proj_1", merged_change)
        assert len(knowledge.notes) == 0

        open_change = PrChange(
            number=2,
            state="open",
            merged_at=None,
            head_branch="feature2",
            agent="coder",
            files=["model.py"],
        )
        await observer.changed("proj_1", open_change)
        assert len(knowledge.notes) == 0
