"""Tests for the table-detail route (/api/projects/{id}/connections/{con}/tables/{table})."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from urllib.parse import quote

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, DuckDbSource
from mlops_agents.data import cache as cache_module
from mlops_agents.data import summaries as summaries_module
from mlops_agents.data.profile import TableProfile
from tests.data.summary_env import ProfileSpy, csv_rows, time_out_after_the_count, write


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> ProfileSpy:
    """Counts the app's profile computations (the module attribute is read at each call)."""
    profiles = ProfileSpy()
    monkeypatch.setattr(summaries_module, "profile_table", profiles)
    return profiles


@pytest.fixture
async def project_id(client: httpx2.AsyncClient, tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    res = await client.post(
        "/api/projects", json={"source": "local", "path": str(repo), "init_git": True}
    )
    assert res.status_code == 201
    return res.json()["id"]


async def _connect(
    client: httpx2.AsyncClient, project_id: str, folder: Path, files: dict[str, str | bytes]
) -> str:
    for relative, content in files.items():
        write(folder / relative, content)
    res = await client.post(
        f"/api/projects/{project_id}/connections",
        json={"kind": LOCAL_FOLDER_KIND, "name": "sales", "settings": {"path": str(folder)}},
    )
    assert res.status_code == 201
    return res.json()["id"]


def _url(project_id: str, con: str, table: str) -> str:
    return f"/api/projects/{project_id}/connections/{con}/tables/{quote(table, safe='')}"


def _short_full_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """The full profile timeout, as `DataSummaries` and the cache see it."""
    for module in (summaries_module, cache_module):
        monkeypatch.setattr(module, "PROFILE_TIMEOUT_S", seconds)


def _assert_profile_timeout(res: httpx2.Response) -> None:
    assert res.status_code == 504
    assert res.json()["code"] == "profile_timeout"
    assert res.json()["message"] == summaries_module.PROFILE_TIMEOUT_MESSAGE


async def test_a_table_detail_is_served_from_the_cache(
    app: FastAPI,
    client: httpx2.AsyncClient,
    project_id: str,
    tmp_path: Path,
    spy: ProfileSpy,
) -> None:
    con = await _connect(client, project_id, tmp_path / "team data", {"people.csv": csv_rows(3)})

    res = await client.get(_url(project_id, con, "people"))

    assert res.status_code == 200
    body = res.json()
    assert body["table"]["name"] == "people"
    assert body["columns"] == [
        {"name": "id", "type": "BIGINT"},
        {"name": "name", "type": "VARCHAR"},
    ]
    assert body["row_count"] == 3
    assert body["timed_out"] is False
    connections = app.state.services.connections
    assert body["data_version"] == await connections.data_version(project_id, con, "people")
    assert '["1", "n1"]' in body["summary"]
    assert spy.count("people.csv") == 1

    again = await client.get(_url(project_id, con, "people"))

    assert again.status_code == 200
    assert again.json() == body
    assert spy.count("people.csv") == 1


async def test_an_unknown_project_is_a_404(client: httpx2.AsyncClient) -> None:
    res = await client.get(_url("prj_missing1", "con_missing1", "people"))
    assert res.status_code == 404
    assert res.json()["code"] == "project_not_found"


async def test_an_unknown_connection_is_a_404(
    client: httpx2.AsyncClient, project_id: str, spy: ProfileSpy
) -> None:
    res = await client.get(_url(project_id, "con_missing1", "people"))
    assert res.status_code == 404
    assert res.json()["code"] == "connection_not_found"
    assert "con_missing1" not in res.text
    assert spy.total == 0


async def test_an_unknown_table_is_a_404(
    client: httpx2.AsyncClient, project_id: str, tmp_path: Path, spy: ProfileSpy
) -> None:
    con = await _connect(client, project_id, tmp_path / "team data", {"people.csv": csv_rows(3)})

    res = await client.get(_url(project_id, con, "orders"))

    assert res.status_code == 404
    assert res.json()["code"] == "table_not_found"
    assert spy.total == 0


async def test_an_unreachable_connection_is_a_409_with_its_reason(
    app: FastAPI,
    client: httpx2.AsyncClient,
    project_id: str,
    tmp_path: Path,
    spy: ProfileSpy,
) -> None:
    folder = tmp_path / "team data"
    con = await _connect(client, project_id, folder, {"people.csv": csv_rows(3)})
    shutil.rmtree(folder)

    res = await client.get(_url(project_id, con, "people"))

    assert res.status_code == 409
    assert res.json()["code"] == "connection_unreachable"
    (view,) = await app.state.services.connections.list(project_id)
    assert view.status_detail
    assert view.status_detail in res.json()["message"]
    assert spy.total == 0


@pytest.mark.parametrize("name", ["people'", "people'; SELECT 1 --", 'x"y'])
async def test_a_table_name_outside_the_listing_is_a_404_and_runs_no_query(
    client: httpx2.AsyncClient, project_id: str, tmp_path: Path, spy: ProfileSpy, name: str
) -> None:
    con = await _connect(client, project_id, tmp_path / "team data", {"people.csv": csv_rows(3)})

    res = await client.get(_url(project_id, con, name))

    assert res.status_code == 404
    assert res.json()["code"] == "table_not_found"
    assert name not in res.text
    assert spy.total == 0


async def test_a_path_as_the_table_name_is_a_404_and_runs_no_query(
    client: httpx2.AsyncClient, project_id: str, tmp_path: Path, spy: ProfileSpy
) -> None:
    """`../x` holds a slash, which the one-segment `{table}` never matches: no route runs."""
    con = await _connect(client, project_id, tmp_path / "team data", {"people.csv": csv_rows(3)})

    res = await client.get(_url(project_id, con, "../x"))

    assert res.status_code == 404
    assert res.json()["code"] == "not_found"
    assert spy.total == 0


async def test_a_corrupt_file_is_a_422(
    client: httpx2.AsyncClient, project_id: str, tmp_path: Path, spy: ProfileSpy
) -> None:
    con = await _connect(
        client, project_id, tmp_path / "team data", {"t.csv": b"a,b\n1,\xff\xfe\n"}
    )

    res = await client.get(_url(project_id, con, "t"))

    assert res.status_code == 422
    assert res.json()["code"] == "table_unreadable"


async def test_a_secret_named_file_is_a_422_and_never_profiled(
    client: httpx2.AsyncClient, project_id: str, tmp_path: Path, spy: ProfileSpy
) -> None:
    token = "ghp_" + "t0ken" * 8
    con = await _connect(
        client, project_id, tmp_path / "team data", {"credentials.json": f'{{"token": "{token}"}}'}
    )

    res = await client.get(_url(project_id, con, "credentials"))

    assert res.status_code == 422
    assert res.json()["code"] == "table_unreadable"
    assert "looks like a secret file" in res.json()["message"]
    assert token not in res.text
    assert spy.total == 0


async def test_a_timed_out_profile_with_columns_answers_with_an_unknown_row_count(
    client: httpx2.AsyncClient,
    project_id: str,
    tmp_path: Path,
    spy: ProfileSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    con = await _connect(client, project_id, tmp_path / "team data", {"people.csv": csv_rows(3)})
    spy.slow.add("people.csv")
    _short_full_timeout(monkeypatch, 0.3)

    res = await client.get(_url(project_id, con, "people"))

    assert res.status_code == 200
    assert res.json()["timed_out"] is True
    assert res.json()["row_count"] is None
    assert res.json()["columns"] == [{"name": "range", "type": "BIGINT"}]


async def test_a_timed_out_profile_keeps_a_row_count_that_finished(
    client: httpx2.AsyncClient,
    project_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #430: `row_count` is null only when the count did not finish."""
    con = await _connect(client, project_id, tmp_path / "team data", {"people.csv": csv_rows(3)})
    time_out_after_the_count(monkeypatch)

    res = await client.get(_url(project_id, con, "people"))

    assert res.status_code == 200
    assert res.json()["timed_out"] is True
    assert res.json()["row_count"] == 3


async def test_a_timed_out_profile_without_columns_is_a_504(
    client: httpx2.AsyncClient,
    project_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    con = await _connect(client, project_id, tmp_path / "team data", {"people.csv": csv_rows(3)})

    async def nothing_computed(source: DuckDbSource, timeout_s: float) -> TableProfile:
        return TableProfile(
            row_count=None,
            columns=[],
            more_columns=0,
            sample_columns=[],
            sample=[],
            timed_out=True,
            timeout_s=timeout_s,
        )

    monkeypatch.setattr(summaries_module, "profile_table", nothing_computed)

    _assert_profile_timeout(await client.get(_url(project_id, con, "people")))


async def test_a_timed_out_wait_on_another_callers_profile_is_a_504(
    app: FastAPI,
    client: httpx2.AsyncClient,
    project_id: str,
    tmp_path: Path,
    spy: ProfileSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    con = await _connect(client, project_id, tmp_path / "team data", {"people.csv": csv_rows(3)})
    (ref,) = await app.state.services.connections.tables(project_id, con)
    spy.slow.add("people.csv")
    first = asyncio.create_task(
        app.state.services.data_summaries.profile(project_id, con, ref, timeout_s=3)
    )
    await spy.started("people.csv")
    _short_full_timeout(monkeypatch, 0.2)

    _assert_profile_timeout(await client.get(_url(project_id, con, "people")))

    assert (await first).timed_out
    assert spy.count("people.csv") == 1
