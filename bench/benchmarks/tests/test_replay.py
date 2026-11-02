from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from benchmarks.cli import main
from benchmarks.datasets import DATASETS
from benchmarks.replay import replay
from benchmarks.scorer import AlreadyScored

from .fixture_dataset import FixtureDataset

DATASETS["fixture"] = FixtureDataset


@pytest.mark.asyncio
async def test_replay_fixture_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "run1"

    # Guard that no connection is made outside 127.0.0.1
    original_connect = socket.socket.connect
    connected_hosts: list[str] = []

    def guarded_connect(sock: socket.socket, address: tuple[str, int] | str) -> None:
        if isinstance(address, tuple):
            host = address[0]
            connected_hosts.append(host)
            assert host in (
                "127.0.0.1",
                "localhost",
                "::1",
            ), f"Attempted connection beyond 127.0.0.1 to {host}"
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    dataset = FixtureDataset()
    score_res = await replay(dataset, run_dir)

    # 1. Connected only to 127.0.0.1
    assert len(connected_hosts) > 0
    assert all(h in ("127.0.0.1", "localhost", "::1") for h in connected_hosts)

    # 2. Both report files exist
    rep_md = run_dir / "report.md"
    rep_json = run_dir / "report.json"
    assert rep_md.exists()
    assert rep_json.exists()

    rep_data = json.loads(rep_json.read_text(encoding="utf-8"))
    assert rep_data["info"]["mode"] == "replay"
    assert rep_data["info"]["dataset"] == "fixture"

    # 3. Scored real_issue on data_quality, no silence raised
    assert len(score_res.matched) == 1
    (inc_id, problem) = next(iter(score_res.matched.items()))
    assert problem.category == "data_quality"
    assert problem.kind == "null_spike"
    assert score_res.verdicts[inc_id].value == "real_issue"
    assert len(score_res.misses) == 0
    assert len(score_res.excluded) == 0

    # Verify no feed silence incidents exist in report
    for p in score_res.matched.values():
        assert p.category == "data_quality"

    # 4. A second replay into the same folder refuses
    with pytest.raises(AlreadyScored):
        await replay(dataset, run_dir)


def test_cli_replay_fixture_and_second_run_refusal(tmp_path: Path) -> None:
    run_dir = tmp_path / "cli_run"
    code1 = main(["replay", "fixture", "--run-dir", str(run_dir)])
    assert code1 == 0
    assert (run_dir / "report.md").exists()
    assert (run_dir / "report.json").exists()

    # Second run refuses
    code2 = main(["replay", "fixture", "--run-dir", str(run_dir)])
    assert code2 == 1
