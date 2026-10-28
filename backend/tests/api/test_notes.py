"""Tests for project knowledge notes API routes."""

from __future__ import annotations

from pathlib import Path

import httpx2
from fastapi import FastAPI

from mlops_agents.knowledge.limits import TEXT_MAX_CHARS, TITLE_MAX_CHARS
from tests.api.conftest import MakeClient, add_local_project


async def test_create_note(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    resp = await client.post(
        f"/api/projects/{project_id}/notes",
        json={
            "type": "fact",
            "title": "PostgreSQL connection pooling",
            "text": "PgBouncer is used on port 6432 with transaction pooling.",
            "source": "https://wiki.internal/db",
        },
    )
    assert resp.status_code == 201
    data = resp.json()

    assert data["id"].startswith("kno_")
    assert data["type"] == "fact"
    assert data["title"] == "PostgreSQL connection pooling"
    assert data["text"] == "PgBouncer is used on port 6432 with transaction pooling."
    assert data["source"] == "https://wiki.internal/db"
    assert data["author"] == "user"
    assert "created" in data
    assert "updated" in data
    assert data["created"] == data["updated"]


async def test_list_newest_notes_and_pagination(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # Create 3 notes sequentially
    created_ids: list[str] = []
    for i in range(3):
        r = await client.post(
            f"/api/projects/{project_id}/notes",
            json={
                "type": "fact",
                "title": f"Note {i}",
                "text": f"Content for note {i}",
            },
        )
        assert r.status_code == 201
        created_ids.append(r.json()["id"])

    # GET without query param returns SearchResult with newest first
    resp = await client.get(f"/api/projects/{project_id}/notes")
    assert resp.status_code == 200
    res = resp.json()
    assert res["used_vectors"] is False
    assert res["detail"] is None
    notes = res["notes"]
    assert len(notes) == 3
    # Newest first: created_ids[2], created_ids[1], created_ids[0]
    assert [n["id"] for n in notes] == [created_ids[2], created_ids[1], created_ids[0]]

    # Limit clamping
    resp_limit = await client.get(f"/api/projects/{project_id}/notes?limit=2")
    assert resp_limit.status_code == 200
    res_limit = resp_limit.json()
    assert len(res_limit["notes"]) == 2
    assert [n["id"] for n in res_limit["notes"]] == [created_ids[2], created_ids[1]]


async def test_search_notes(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # Note 1: Postgres fact
    r1 = await client.post(
        f"/api/projects/{project_id}/notes",
        json={
            "type": "fact",
            "title": "Postgres configuration tuning",
            "text": "Shared buffers set to 25% of total memory.",
        },
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    # Note 2: Redis incident
    r2 = await client.post(
        f"/api/projects/{project_id}/notes",
        json={
            "type": "incident",
            "title": "Redis memory exhaustion",
            "text": "Maxmemory limit was reached causing evictions.",
        },
    )
    assert r2.status_code == 201
    id2 = r2.json()["id"]

    # Note 3: Postgres incident
    r3 = await client.post(
        f"/api/projects/{project_id}/notes",
        json={
            "type": "incident",
            "title": "Postgres replication lag",
            "text": "High write throughput delayed standby replica.",
        },
    )
    assert r3.status_code == 201
    id3 = r3.json()["id"]

    # Search with q=Postgres should find Note 1 and Note 3
    resp = await client.get(f"/api/projects/{project_id}/notes?q=Postgres")
    assert resp.status_code == 200
    res = resp.json()
    hit_ids = [n["id"] for n in res["notes"]]
    assert id1 in hit_ids
    assert id3 in hit_ids
    assert id2 not in hit_ids

    # Search with q=Postgres & type=incident should only find Note 3
    resp_typed = await client.get(f"/api/projects/{project_id}/notes?q=Postgres&type=incident")
    assert resp_typed.status_code == 200
    res_typed = resp_typed.json()
    hit_typed_ids = [n["id"] for n in res_typed["notes"]]
    assert hit_typed_ids == [id3]


async def test_patch_note(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    create_resp = await client.post(
        f"/api/projects/{project_id}/notes",
        json={
            "type": "fact",
            "title": "Original Title",
            "text": "Original text content",
            "source": "https://example.com/original",
        },
    )
    assert create_resp.status_code == 201
    note = create_resp.json()
    note_id = note["id"]
    original_created = note["created"]
    original_author = note["author"]

    # Patch only title
    patch_resp = await client.patch(
        f"/api/projects/{project_id}/notes/{note_id}",
        json={"title": "Updated Title"},
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()

    assert updated["id"] == note_id
    assert updated["title"] == "Updated Title"
    assert updated["text"] == "Original text content"
    assert updated["source"] == "https://example.com/original"
    assert updated["type"] == "fact"
    assert updated["author"] == original_author
    assert updated["created"] == original_created
    assert updated["updated"] >= original_created

    # Patch type and text
    patch_resp2 = await client.patch(
        f"/api/projects/{project_id}/notes/{note_id}",
        json={"type": "decision", "text": "Updated text content"},
    )
    assert patch_resp2.status_code == 200
    updated2 = patch_resp2.json()

    assert updated2["title"] == "Updated Title"
    assert updated2["type"] == "decision"
    assert updated2["text"] == "Updated text content"
    assert updated2["author"] == original_author


async def test_delete_note(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    create_resp = await client.post(
        f"/api/projects/{project_id}/notes",
        json={
            "type": "runbook",
            "title": "Rollback procedure",
            "text": "Step 1: drain traffic. Step 2: switch DNS.",
        },
    )
    assert create_resp.status_code == 201
    note_id = create_resp.json()["id"]

    # Delete returns 204
    del_resp = await client.delete(f"/api/projects/{project_id}/notes/{note_id}")
    assert del_resp.status_code == 204

    # Subsequent GET shows it is gone
    list_resp = await client.get(f"/api/projects/{project_id}/notes")
    assert list_resp.status_code == 200
    assert not any(n["id"] == note_id for n in list_resp.json()["notes"])

    # Subsequent DELETE returns 404
    del_again = await client.delete(f"/api/projects/{project_id}/notes/{note_id}")
    assert del_again.status_code == 404
    assert del_again.json()["code"] == "note_not_found"

    # Subsequent PATCH returns 404
    patch_del = await client.patch(
        f"/api/projects/{project_id}/notes/{note_id}",
        json={"title": "Should fail"},
    )
    assert patch_del.status_code == 404
    assert patch_del.json()["code"] == "note_not_found"


async def test_project_isolation(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    proj_a = await add_local_project(app, tmp_path / "repo_a", git_env)
    proj_b = await add_local_project(app, tmp_path / "repo_b", git_env)

    # Create note in project A
    resp_a = await client.post(
        f"/api/projects/{proj_a}/notes",
        json={"type": "fact", "title": "Secret in A", "text": "Confidential to A"},
    )
    assert resp_a.status_code == 201
    note_a_id = resp_a.json()["id"]

    # Project B cannot see Note A
    list_b = await client.get(f"/api/projects/{proj_b}/notes")
    assert list_b.status_code == 200
    assert list_b.json()["notes"] == []

    search_b = await client.get(f"/api/projects/{proj_b}/notes?q=Secret")
    assert search_b.status_code == 200
    assert search_b.json()["notes"] == []

    # Project B cannot patch or delete Note A
    patch_b = await client.patch(
        f"/api/projects/{proj_b}/notes/{note_a_id}",
        json={"title": "Hacked"},
    )
    assert patch_b.status_code == 404
    assert patch_b.json()["code"] == "note_not_found"

    delete_b = await client.delete(f"/api/projects/{proj_b}/notes/{note_a_id}")
    assert delete_b.status_code == 404
    assert delete_b.json()["code"] == "note_not_found"


async def test_404_errors(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # Unknown project
    resp_unk_proj = await client.post(
        "/api/projects/prj_unknown/notes",
        json={"type": "fact", "title": "A", "text": "B"},
    )
    assert resp_unk_proj.status_code == 404
    assert resp_unk_proj.json()["code"] == "project_not_found"

    resp_unk_list = await client.get("/api/projects/prj_unknown/notes")
    assert resp_unk_list.status_code == 404
    assert resp_unk_list.json()["code"] == "project_not_found"

    resp_unk_patch = await client.patch(
        "/api/projects/prj_unknown/notes/kno_123",
        json={"title": "A"},
    )
    assert resp_unk_patch.status_code == 404
    assert resp_unk_patch.json()["code"] == "project_not_found"

    resp_unk_del = await client.delete("/api/projects/prj_unknown/notes/kno_123")
    assert resp_unk_del.status_code == 404
    assert resp_unk_del.json()["code"] == "project_not_found"

    # Unknown note ID (valid format starting with kno_)
    patch_unk_note = await client.patch(
        f"/api/projects/{project_id}/notes/kno_doesnotexist",
        json={"title": "A"},
    )
    assert patch_unk_note.status_code == 404
    assert patch_unk_note.json()["code"] == "note_not_found"

    del_unk_note = await client.delete(f"/api/projects/{project_id}/notes/kno_doesnotexist")
    assert del_unk_note.status_code == 404
    assert del_unk_note.json()["code"] == "note_not_found"

    # Malformed note ID (not starting with kno_)
    patch_bad_note = await client.patch(
        f"/api/projects/{project_id}/notes/bad_note_id",
        json={"title": "A"},
    )
    assert patch_bad_note.status_code == 404
    assert patch_bad_note.json()["code"] == "note_not_found"

    del_bad_note = await client.delete(f"/api/projects/{project_id}/notes/bad_note_id")
    assert del_bad_note.status_code == 404
    assert del_bad_note.json()["code"] == "note_not_found"


async def test_422_errors(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # Empty title
    r_empty_title = await client.post(
        f"/api/projects/{project_id}/notes",
        json={"type": "fact", "title": "   ", "text": "Valid text"},
    )
    assert r_empty_title.status_code == 422
    assert r_empty_title.json()["code"] == "invalid_note"

    # Empty text
    r_empty_text = await client.post(
        f"/api/projects/{project_id}/notes",
        json={"type": "fact", "title": "Valid title", "text": "   "},
    )
    assert r_empty_text.status_code == 422
    assert r_empty_text.json()["code"] == "invalid_note"

    # Text exceeding TEXT_MAX_CHARS
    r_long_text = await client.post(
        f"/api/projects/{project_id}/notes",
        json={"type": "fact", "title": "Valid title", "text": "a" * (TEXT_MAX_CHARS + 1)},
    )
    assert r_long_text.status_code == 422
    assert r_long_text.json()["code"] == "note_too_long"

    # Title exceeding TITLE_MAX_CHARS
    r_long_title = await client.post(
        f"/api/projects/{project_id}/notes",
        json={"type": "fact", "title": "a" * (TITLE_MAX_CHARS + 1), "text": "Valid text"},
    )
    assert r_long_title.status_code == 422
    assert r_long_title.json()["code"] == "note_too_long"

    # Create a valid note for patch tests
    r_valid = await client.post(
        f"/api/projects/{project_id}/notes",
        json={"type": "fact", "title": "Valid", "text": "Valid"},
    )
    assert r_valid.status_code == 201
    note_id = r_valid.json()["id"]

    # Empty patch (no fields provided)
    r_empty_patch = await client.patch(
        f"/api/projects/{project_id}/notes/{note_id}",
        json={},
    )
    assert r_empty_patch.status_code == 422
    assert r_empty_patch.json()["code"] == "empty_patch"

    # Empty title in patch
    r_patch_empty_title = await client.patch(
        f"/api/projects/{project_id}/notes/{note_id}",
        json={"title": "   "},
    )
    assert r_patch_empty_title.status_code == 422
    assert r_patch_empty_title.json()["code"] == "invalid_note"

    # Text exceeding TEXT_MAX_CHARS in patch
    r_patch_long_text = await client.patch(
        f"/api/projects/{project_id}/notes/{note_id}",
        json={"text": "x" * (TEXT_MAX_CHARS + 1)},
    )
    assert r_patch_long_text.status_code == 422
    assert r_patch_long_text.json()["code"] == "note_too_long"

    # Extra forbidden field in patch
    r_patch_extra = await client.patch(
        f"/api/projects/{project_id}/notes/{note_id}",
        json={"unknown_field": "val"},
    )
    assert r_patch_extra.status_code == 422


async def test_secret_scrubbing_through_route(
    make_client: MakeClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    secret_token = "my-secret-access-token"
    async with make_client("local", MLOPS_AGENTS_ACCESS_TOKEN=secret_token) as client:
        repo_dir = tmp_path / "scrub_repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        from tests.conftest import make_repo

        repo = make_repo(repo_dir, {"README.md": "# Scrub"}, git_env)
        resp_proj = await client.post(
            "/api/projects",
            json={"source": "local", "path": str(repo)},
        )
        assert resp_proj.status_code == 201
        project_id = resp_proj.json()["id"]

        # Post note with secret access token and URL secrets
        payload = {
            "type": "fact",
            "title": f"Secrets in URL: https://admin:{secret_token}@db.example.com",
            "text": f"App token is {secret_token}, see https://api.internal/v1?token=secret123",
            "source": "https://user:pass@wiki.internal?key=val456",
        }
        resp = await client.post(f"/api/projects/{project_id}/notes", json=payload)
        assert resp.status_code == 201
        data = resp.json()

        # Check scrubbed fields
        assert secret_token not in data["title"]
        assert secret_token not in data["text"]
        assert "secret123" not in data["text"]
        assert "pass@" not in data["source"]
        assert "val456" not in data["source"]

        assert "***@" in data["title"]
        assert "***" in data["text"]

        # Check GET returns scrubbed data as well
        get_resp = await client.get(f"/api/projects/{project_id}/notes")
        assert get_resp.status_code == 200
        first_note = get_resp.json()["notes"][0]
        assert secret_token not in first_note["text"]
        assert "secret123" not in first_note["text"]
