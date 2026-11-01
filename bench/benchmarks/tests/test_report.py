from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from mlops_agents.monitoring.verdicts.contract import VerdictDraft
from mlops_agents.stats.contract import StatsView, StatValue

from benchmarks.cli import main
from benchmarks.report import (
    MODE_GROUPS,
    Pause,
    RunInfo,
    format_comparison_table,
    report_title,
    write_report,
)
from benchmarks.scorer import Problem, Score


class FakeBenchClient:
    def __init__(
        self,
        stats_responses: dict[str, StatsView] | None = None,
        usage_response: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> None:
        self.stats_responses = stats_responses or {}
        self.usage_response = usage_response
        self.token = token
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        query = dict(params) if params is not None else None
        self.calls.append((path, query))
        if path == "/usage":
            return self.usage_response
        if "stats" in path:
            cat = query.get("category") if query else None
            key = cat if cat else "overall"
            res = self.stats_responses.get(key)
            if res is not None:
                return res.model_dump(mode="json")
            return self._make_default_view().model_dump(mode="json")
        return None

    async def post(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append((path, None))
        return None

    def _make_default_view(self) -> StatsView:
        return StatsView(
            window="all",
            category=None,
            baseline_available=True,
            groups={
                "detection": {
                    "precision": StatValue(value=0.8, n=10, collecting=False),
                    "caught_baseline_missed": StatValue(value=1.0, n=2, collecting=False),
                    "false_alarms_avoided": StatValue(value=1.0, n=1, collecting=False),
                    "misses": StatValue(value=2.0, n=2, collecting=False),
                },
                "speed": {
                    "detection_delay_s": StatValue(value=45.0, n=8, collecting=False),
                    "time_to_resolution_s": StatValue(value=120.0, n=5, collecting=False),
                },
                "noise": {
                    "alerts_per_week": StatValue(value=2.5, n=10, collecting=False),
                    "signals_per_incident": StatValue(value=1.1, n=8, collecting=False),
                },
                "diagnosis": {
                    "bad_data_vs_drift_correct": StatValue(value=1.0, n=8, collecting=False),
                },
                "effort": {
                    "autonomous_share": StatValue(value=0.95, n=10, collecting=False),
                    "approvals_requested": StatValue(value=2.0, n=2, collecting=False),
                    "acceptance_rate": StatValue(value=1.0, n=2, collecting=False),
                    "prs_opened": StatValue(value=2.0, n=2, collecting=False),
                    "prs_merged": StatValue(value=2.0, n=2, collecting=False),
                    "prs_closed": StatValue(value=0.0, n=0, collecting=False),
                },
                "model_impact": {
                    "metric_before": StatValue(value=0.7, n=4, collecting=False),
                    "metric_after": StatValue(value=0.85, n=4, collecting=False),
                },
                "cost": {
                    "tokens_per_agent": StatValue(value=30000.0, n=3, collecting=False),
                    "cost_per_incident": StatValue(value=0.08, n=8, collecting=False),
                },
                "reliability": {
                    "failed_runs": StatValue(value=0.0, n=0, collecting=False),
                    "tool_errors": StatValue(value=0.0, n=0, collecting=False),
                    "sandbox_failures": StatValue(value=0.0, n=0, collecting=False),
                },
            },
        )


def _make_sample_score() -> Score:
    p1 = Problem(
        kind="null_spike",
        category="data_quality",
        start=date(2026, 7, 1),
        end=date(2026, 7, 3),
        columns=("amount",),
    )
    p2 = Problem(
        kind="covariate_shift",
        category="drift",
        start=date(2026, 7, 10),
        end=date(2026, 7, 12),
        columns=("v1",),
    )
    return Score(
        matched={"inc_1": p1},
        verdicts={"inc_1": VerdictDraft(value="real_issue")},
        misses=[p2],
        silent={},
        excluded=[],
    )


@pytest.mark.asyncio
async def test_replay_mode_groups_not_measured(tmp_path: Path) -> None:
    """A replay-mode report never prints effort, cost, model impact or reliability

    values and lists them as not measured.
    """
    client = FakeBenchClient()
    score = _make_sample_score()
    run_info = RunInfo(
        mode="replay",
        dataset="ulb",
        scenario="ulb.toml",
        seed=42,
        app_commit="242335cd",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 5, tzinfo=UTC),
        provider=None,
        model=None,
        seconds_per_day=None,
        pauses=[],
        day_gaps=[],
    )

    md_path, json_path = await write_report(client, "prj_1", tmp_path, run_info, score)
    assert md_path.exists()
    assert json_path.exists()

    md_text = md_path.read_text(encoding="utf-8")

    # In replay mode: effort, cost, model_impact, reliability must be listed as "not measured in this mode"
    for group_name in ["effort", "cost", "model_impact", "reliability"]:
        assert group_name not in MODE_GROUPS["replay"]
        assert "not measured in this mode" in md_text

    # Values for those groups (e.g. autonomous_share, tokens_per_agent, metric_before, failed_runs)
    # must NEVER be printed in replay mode
    assert "autonomous_share" not in md_text
    assert "tokens_per_agent" not in md_text
    assert "metric_before" not in md_text
    assert "failed_runs" not in md_text
    assert "cost_per_incident" not in md_text


@pytest.mark.asyncio
async def test_replay_mode_labels(tmp_path: Path) -> None:
    """A replay-mode report labels diagnosis 'category of the check that fired' and

    speed as pickup delay.
    """
    client = FakeBenchClient()
    score = _make_sample_score()
    run_info = RunInfo(
        mode="replay",
        dataset="ulb",
        scenario="ulb.toml",
        seed=42,
        app_commit="242335cd",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 5, tzinfo=UTC),
        provider=None,
        model=None,
        seconds_per_day=None,
        pauses=[],
        day_gaps=[],
    )

    md_path, _ = await write_report(client, "prj_1", tmp_path, run_info, score)
    md_text = md_path.read_text(encoding="utf-8")

    assert "category of the check that fired" in md_text
    assert "pickup delay: first firing result to incident" in md_text


@pytest.mark.asyncio
async def test_agents_mode_omits_metrics_and_labels_pickup_delay(tmp_path: Path) -> None:
    """An agents-mode report omits alerts_per_week and time_to_resolution_s and

    labels the pickup delay.
    """
    client = FakeBenchClient(
        usage_response={
            "rows": [
                {
                    "project_id": "prj_1",
                    "agent": "data-quality",
                    "model_address": "gemini-2.0-flash",
                    "week_start": "2026-10-26",
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "cost_usd": 0.05,
                    "requests": 5,
                }
            ],
            "unpriced_models": [],
        }
    )
    score = _make_sample_score()
    run_info = RunInfo(
        mode="agents",
        dataset="demo-world",
        scenario="short.toml",
        seed=None,
        app_commit="242335cd",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 30, tzinfo=UTC),
        provider="google",
        model="gemini-2.0-flash",
        seconds_per_day=15.0,
        pauses=[],
        day_gaps=[],
    )

    md_path, _ = await write_report(client, "prj_1", tmp_path, run_info, score)
    md_text = md_path.read_text(encoding="utf-8")

    # In agents mode, alerts_per_week and time_to_resolution_s are omitted
    assert "alerts_per_week" not in md_text
    assert "time_to_resolution_s" not in md_text

    # Pickup delay must be labeled
    assert "pickup delay: first firing result to incident" in md_text

    # Effort and cost ARE measured in agents mode
    assert "autonomous_share" in md_text
    assert "tokens_per_agent" in md_text


@pytest.mark.asyncio
async def test_stat_value_formatting(tmp_path: Path) -> None:
    """A collecting value prints as collecting, a None as not available, never 0;

    each stat displays its n.
    """
    view = StatsView(
        window="all",
        category=None,
        baseline_available=True,
        groups={
            "detection": {
                "precision": StatValue(value=None, n=0, collecting=True),
                "caught_baseline_missed": StatValue(value=None, n=3, collecting=False),
                "false_alarms_avoided": StatValue(value=0.0, n=5, collecting=False),
                "misses": StatValue(value=4.0, n=4, collecting=False),
            },
            "speed": {
                "detection_delay_s": StatValue(value=55.25, n=6, collecting=False),
                "time_to_resolution_s": StatValue(value=None, n=0, collecting=False),
            },
            "noise": {
                "alerts_per_week": StatValue(value=1.5, n=2, collecting=False),
                "signals_per_incident": StatValue(value=None, n=1, collecting=True),
            },
            "diagnosis": {
                "bad_data_vs_drift_correct": StatValue(value=1.0, n=2, collecting=False),
            },
            "effort": {},
            "model_impact": {},
            "cost": {},
            "reliability": {},
        },
    )
    client = FakeBenchClient(stats_responses={"overall": view})
    score = _make_sample_score()
    run_info = RunInfo(
        mode="replay",
        dataset="ulb",
        scenario="ulb.toml",
        seed=1,
        app_commit="abc1234",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 10, tzinfo=UTC),
        provider=None,
        model=None,
        seconds_per_day=None,
        pauses=[],
        day_gaps=[],
    )

    md_path, _ = await write_report(client, "prj_1", tmp_path, run_info, score)
    md_text = md_path.read_text(encoding="utf-8")

    # Precision is collecting: must show collecting, must show n=0, must not show 0 or 0.00
    assert "collecting (n=0)" in md_text

    # caught_baseline_missed is None: must show not available, n=3, not 0
    assert "not available (n=3)" in md_text

    # false_alarms_avoided is genuinely 0.0: prints 0.000 or 0.0 (n=5)
    assert "(n=5)" in md_text

    # detection_delay_s displays its value and n=6
    assert "(n=6)" in md_text


@pytest.mark.asyncio
async def test_agents_mode_usage_pauses_and_planted_flaws(tmp_path: Path) -> None:
    """Usage, pauses, and planted flaws checklist appear in agents mode."""
    p1 = Pause(
        reason="ready_wait",
        start=datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC),
        end=datetime(2026, 11, 1, 10, 4, 30, tzinfo=UTC),
    )
    p2 = Pause(
        reason="model_stall",
        start=datetime(2026, 11, 1, 10, 20, 0, tzinfo=UTC),
        end=datetime(2026, 11, 1, 10, 22, 15, tzinfo=UTC),
    )
    score = _make_sample_score()
    run_info = RunInfo(
        mode="agents",
        dataset="demo-world",
        scenario="short.toml",
        seed=None,
        app_commit="242335cd",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 11, 0, tzinfo=UTC),
        provider="google",
        model="gemini-2.0-flash",
        seconds_per_day=10.0,
        pauses=[p1, p2],
        day_gaps=[date(2026, 11, 3)],
    )

    client = FakeBenchClient(
        usage_response={
            "rows": [
                {
                    "project_id": "prj_1",
                    "agent": "data-quality",
                    "model_address": "gemini-2.0-flash",
                    "week_start": "2026-10-26",
                    "prompt_tokens": 15000,
                    "completion_tokens": 3000,
                    "cost_usd": 0.45,
                    "requests": 12,
                },
                {
                    "project_id": "prj_1",
                    "agent": "drift",
                    "model_address": "gemini-2.0-flash",
                    "week_start": "2026-10-26",
                    "prompt_tokens": 8000,
                    "completion_tokens": 1200,
                    "cost_usd": 0.22,
                    "requests": 7,
                },
            ],
            "unpriced_models": [],
        }
    )

    md_path, json_path = await write_report(client, "prj_1", tmp_path, run_info, score)
    md_text = md_path.read_text(encoding="utf-8")

    # Title names the mode
    assert "Agents Mode" in md_text

    # Pauses listed with reason and duration
    assert "readiness wait" in md_text.lower() or "ready_wait" in md_text.lower()
    assert "model stall" in md_text.lower() or "model_stall" in md_text.lower()
    assert "270" in md_text  # 4m30s = 270s
    assert "135" in md_text  # 2m15s = 135s

    # Usage section present
    assert "Usage" in md_text
    assert "data-quality" in md_text
    assert "drift" in md_text
    assert "18000" in md_text or "18,000" in md_text  # 15000 + 3000

    # Planted flaws checklist
    assert "- [x]" in md_text
    assert "- [ ]" in md_text
    assert "null_spike" in md_text
    assert "covariate_shift" in md_text

    # JSON contains everything
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["info"]["mode"] == "agents"
    assert len(data["info"]["pauses"]) == 2
    assert "overall_stats" in data
    assert "category_stats" in data
    assert "score" in data
    assert "usage" in data


@pytest.mark.asyncio
async def test_no_access_token_in_either_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No access token appears in report.md or report.json (grepped)."""
    secret_token = "secret-token-super-private-xyz-987654321"
    monkeypatch.setenv("MLOPS_AGENTS_ACCESS_TOKEN", secret_token)

    client = FakeBenchClient(token=secret_token)
    score = _make_sample_score()
    run_info = RunInfo(
        mode="replay",
        dataset="ulb",
        scenario="ulb.toml",
        seed=42,
        app_commit="commit-with-no-token",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 5, tzinfo=UTC),
        provider=None,
        model=None,
        seconds_per_day=None,
        pauses=[],
        day_gaps=[],
    )

    md_path, json_path = await write_report(client, "prj_1", tmp_path, run_info, score)
    md_content = md_path.read_text(encoding="utf-8")
    json_content = json_path.read_text(encoding="utf-8")

    assert secret_token not in md_content
    assert secret_token not in json_content


@pytest.mark.asyncio
async def test_bench_report_cli_two_reports_one_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """bench report on two report.json files gives one table."""
    client = FakeBenchClient()
    score = _make_sample_score()

    # Create run 1 (replay)
    run1_dir = tmp_path / "run_replay"
    info1 = RunInfo(
        mode="replay",
        dataset="ulb",
        scenario="ulb.toml",
        seed=1,
        app_commit="commit1",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 2, tzinfo=UTC),
        provider=None,
        model=None,
        seconds_per_day=None,
        pauses=[],
        day_gaps=[],
    )
    await write_report(client, "prj_1", run1_dir, info1, score)

    # Create run 2 (agents)
    run2_dir = tmp_path / "run_agents"
    info2 = RunInfo(
        mode="agents",
        dataset="demo-world",
        scenario="short.toml",
        seed=None,
        app_commit="commit2",
        started=datetime(2026, 11, 1, 11, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 11, 30, tzinfo=UTC),
        provider="google",
        model="gemini-2.0-flash",
        seconds_per_day=20.0,
        pauses=[],
        day_gaps=[],
    )
    await write_report(client, "prj_1", run2_dir, info2, score)

    # Test direct python function
    table = format_comparison_table([run1_dir / "report.json", run2_dir / "report.json"])
    assert "replay" in table
    assert "agents" in table
    assert "ulb" in table
    assert "demo-world" in table

    # Test CLI invocation: bench report <run1_dir> <run2_dir>
    exit_code = main(["report", str(run1_dir), str(run2_dir)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "replay" in captured.out
    assert "agents" in captured.out
    assert "ulb" in captured.out
    assert "demo-world" in captured.out


@pytest.mark.asyncio
async def test_title_with_stall(tmp_path: Path) -> None:
    """An agents run with a stall says so in its title."""
    client = FakeBenchClient()
    score = _make_sample_score()

    # 1. Agents run with model_stall
    stall_pause = Pause(
        reason="model_stall",
        start=datetime(2026, 11, 1, 10, 5, tzinfo=UTC),
        end=datetime(2026, 11, 1, 10, 7, tzinfo=UTC),
    )
    info_stalled = RunInfo(
        mode="agents",
        dataset="demo-world",
        scenario="short.toml",
        seed=None,
        app_commit="commit1",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 30, tzinfo=UTC),
        provider="google",
        model="gemini-2.0-flash",
        seconds_per_day=None,
        pauses=[stall_pause],
        day_gaps=[],
    )
    assert report_title(info_stalled) == "Benchmark Report: Agents Mode (stalled)"

    stalled_dir = tmp_path / "stalled"
    md_path, json_path = await write_report(client, "prj_1", stalled_dir, info_stalled, score)
    md_text = md_path.read_text(encoding="utf-8")
    assert "# Benchmark Report: Agents Mode (stalled)" in md_text

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["title"] == "Benchmark Report: Agents Mode (stalled)"
    assert data["info"]["title"] == "Benchmark Report: Agents Mode (stalled)"

    # 2. Agents run with only ready_wait (no stall)
    ready_pause = Pause(
        reason="ready_wait",
        start=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 11, 1, 10, 2, tzinfo=UTC),
    )
    info_no_stall = RunInfo(
        mode="agents",
        dataset="demo-world",
        scenario="short.toml",
        seed=None,
        app_commit="commit1",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 30, tzinfo=UTC),
        provider="google",
        model="gemini-2.0-flash",
        seconds_per_day=None,
        pauses=[ready_pause],
        day_gaps=[],
    )
    assert report_title(info_no_stall) == "Benchmark Report: Agents Mode"

    no_stall_dir = tmp_path / "no_stall"
    md_path, json_path = await write_report(client, "prj_1", no_stall_dir, info_no_stall, score)
    md_text = md_path.read_text(encoding="utf-8")
    assert "# Benchmark Report: Agents Mode\n" in md_text
    assert "(stalled)" not in md_text

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["title"] == "Benchmark Report: Agents Mode"


@pytest.mark.asyncio
async def test_problem_deduplication(tmp_path: Path) -> None:
    """Distinct problems are deduplicated across multiple matching incidents."""
    p1 = Problem(
        kind="null_spike",
        category="data_quality",
        start=date(2026, 7, 1),
        end=date(2026, 7, 3),
        columns=("amount",),
    )
    p2 = Problem(
        kind="covariate_shift",
        category="drift",
        start=date(2026, 7, 10),
        end=date(2026, 7, 12),
        columns=("v1",),
    )
    # Two incidents matched to the SAME problem p1
    score = Score(
        matched={"inc_1": p1, "inc_2": p1},
        verdicts={
            "inc_1": VerdictDraft(value="real_issue"),
            "inc_2": VerdictDraft(value="real_issue"),
        },
        misses=[p2],
        silent={},
        excluded=[],
    )
    client = FakeBenchClient()
    run_info = RunInfo(
        mode="agents",
        dataset="demo-world",
        scenario="short.toml",
        seed=None,
        app_commit="commit1",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 10, tzinfo=UTC),
        provider="google",
        model="gemini-2.0-flash",
        seconds_per_day=None,
        pauses=[],
        day_gaps=[],
    )

    md_path, json_path = await write_report(client, "prj_1", tmp_path, run_info, score)
    md_text = md_path.read_text(encoding="utf-8")

    # In problem counts: data_quality must be 1, not 2
    assert "- **data_quality:** 1" in md_text
    assert "- **drift:** 1" in md_text

    # In planted flaws checklist: null_spike must appear exactly once under - [x]
    assert md_text.count("- [x] null_spike") == 1
    assert md_text.count("- [ ] covariate_shift") == 1

    # In comparison table: caught is 1/2, not 2/3
    table = format_comparison_table([json_path])
    assert "1/2" in table
    assert "2/3" not in table


@pytest.mark.asyncio
async def test_usage_without_requests_field(tmp_path: Path) -> None:
    """When /usage lacks a requests field (the real contract), it reports 'not available' rather than fabricating 1."""
    client = FakeBenchClient(
        usage_response={
            "rows": [
                {
                    "project_id": "prj_1",
                    "agent": "data-quality",
                    "model_address": "gemini-2.0-flash",
                    "week_start": "2026-10-26",
                    "prompt_tokens": 10000,
                    "completion_tokens": 2000,
                    "cost_usd": 0.30,
                    # Note: No 'requests' field!
                }
            ],
            "unpriced_models": [],
        }
    )
    score = _make_sample_score()
    run_info = RunInfo(
        mode="agents",
        dataset="demo-world",
        scenario="short.toml",
        seed=None,
        app_commit="commit1",
        started=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 11, 1, 10, 10, tzinfo=UTC),
        provider="google",
        model="gemini-2.0-flash",
        seconds_per_day=None,
        pauses=[],
        day_gaps=[],
    )

    md_path, _ = await write_report(client, "prj_1", tmp_path, run_info, score)
    md_text = md_path.read_text(encoding="utf-8")

    # Total requests must be not available, never 1
    assert "- **Total Requests:** not available" in md_text
    # In table, requests shows N/A
    assert "| data-quality | gemini-2.0-flash | N/A | 12,000 | $0.3000 |" in md_text
