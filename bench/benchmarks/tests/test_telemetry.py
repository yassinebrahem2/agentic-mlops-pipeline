"""Tests for batch pipeline telemetry dataset, scenario, and injections."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from mlops_agents.api.app import create_app
from mlops_agents.config import RunMode, load_settings
from mlops_agents.monitoring.settings import MonitoringSettings

from benchmarks.datasets.contract import Dataset
from benchmarks.datasets.telemetry import (
    DEFAULT_SCENARIO_PATH,
    LateArrivalInjection,
    MissingFeedInjection,
    ParserFailureInjection,
    TelemetryDataset,
    load_telemetry_scenario,
    telemetry_business_days,
)
from benchmarks.replay import replay


def test_telemetry_dataset_implements_protocol() -> None:
    ds = TelemetryDataset(seed=42)
    dataset_protocol: Dataset = ds
    assert dataset_protocol.name == "telemetry"
    assert dataset_protocol.scenario == "telemetry"
    assert dataset_protocol.seed == 42
    assert len(dataset_protocol.days()) == 60
    assert callable(ds.handoff)
    assert callable(ds.prepare)
    assert callable(ds.write_day)
    assert callable(ds.key)

    handoff = ds.handoff()
    assert handoff.operations.feeds, "Handoff must declare feeds"
    assert handoff.operations.results_location is not None, "Handoff must declare results location"
    assert handoff.operations.checks, "Handoff must declare checks"
    assert handoff.data_roles, "Handoff must declare data roles"

    key = ds.key()
    assert len(key) >= 5, f"Expected at least 5 answer key records, got {len(key)}"
    assert all(r.category in ("data_quality", "operational") for r in key), (
        "All telemetry records must have data_quality or operational category"
    )


def test_telemetry_seed_reproducibility(tmp_path: Path) -> None:
    dir1 = tmp_path / "run1"
    dir2 = tmp_path / "run2"
    data1, res1 = dir1 / "data", dir1 / "results"
    data2, res2 = dir2 / "data", dir2 / "results"

    ds1 = TelemetryDataset(seed=99)
    ds2 = TelemetryDataset(seed=99)
    day = ds1.days()[0]

    ds1.write_day(day, data1, res1)
    ds2.write_day(day, data2, res2)

    files1 = {p.name: p.read_bytes() for p in data1.rglob("*") if p.is_file()}
    files2 = {p.name: p.read_bytes() for p in data2.rglob("*") if p.is_file()}
    assert files1 == files2, "Identical seeds must produce identical data files"


def test_telemetry_scenario_spacing_and_counts() -> None:
    scenario_path = DEFAULT_SCENARIO_PATH
    assert scenario_path.is_file(), f"Scenario file not found: {scenario_path}"

    injections = load_telemetry_scenario(scenario_path)
    assert len(injections) >= 5, f"Expected at least 5 problems, got {len(injections)}"

    # Check presence of all 3 operational problem types
    missing_injs = [inj for inj in injections if isinstance(inj, MissingFeedInjection)]
    late_injs = [inj for inj in injections if isinstance(inj, LateArrivalInjection)]
    parser_injs = [inj for inj in injections if isinstance(inj, ParserFailureInjection)]

    assert len(missing_injs) >= 1, "Must contain at least 1 missing feed problem"
    assert len(late_injs) >= 1, "Must contain at least 1 late arrival problem"
    assert len(parser_injs) >= 1, "Must contain at least 1 parser failure problem"

    all_days = telemetry_business_days(start_date=date(2026, 1, 5), count=60)
    first_day = all_days[0]
    last_day = all_days[-1]
    parser_disable_limit = MonitoringSettings().parser_disable_after

    for inj in injections:
        assert inj.start >= first_day, f"{inj} starts before day 1 ({first_day})"
        assert inj.end <= last_day, f"{inj} ends after day 60 ({last_day})"
        assert inj.category in ("data_quality", "operational")

        b_days = [d for d in all_days if inj.start <= d <= inj.end]
        if isinstance(inj, ParserFailureInjection):
            assert len(b_days) < parser_disable_limit, (
                f"Parser failure window length {len(b_days)} must be strictly less than "
                f"parser_disable_after ({parser_disable_limit})"
            )
            assert inj.category == "operational"
        else:
            assert len(b_days) == 3, f"{inj} must span exactly 3 business days, got {len(b_days)}"
            assert inj.category == "data_quality"

    # Verify each problem starts at least 9 days after previous ends (both calendar and business days >= 9)
    for i in range(len(injections) - 1):
        prev_inj = injections[i]
        next_inj = injections[i + 1]
        calendar_gap = (next_inj.start - prev_inj.end).days
        assert calendar_gap >= 9, (
            f"Problem {i + 1} starts on {next_inj.start}, less than 9 days after problem {i} ends on {prev_inj.end}"
        )
        # Business days gap
        business_gap = len([d for d in all_days if prev_inj.end < d < next_inj.start])
        assert business_gap >= 7, (
            f"Problem {i + 1} business gap {business_gap} must be >= 7 business days"
        )


@pytest.mark.asyncio
async def test_quiet_days_raise_nothing(tmp_path: Path) -> None:
    """Verifies that days without any injection raise 0 signals and 0 incidents."""
    quiet_days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    ds = TelemetryDataset(seed=42, days_override=quiet_days, injections_override=[])

    run_dir = tmp_path / "quiet_run"
    score_res = await replay(ds, run_dir)

    assert len(score_res.matched) == 0
    assert len(score_res.misses) == 0
    assert len(score_res.verdicts) == 0


@pytest.mark.asyncio
async def test_missing_feed_window_raises_silence_scored_real_issue(tmp_path: Path) -> None:
    """A missing-feed window raises one silence incident scored real_issue."""
    days = [
        date(2026, 1, 5),  # normal
        date(2026, 1, 6),  # normal
        date(2026, 1, 7),  # missing start
        date(2026, 1, 8),  # missing
        date(2026, 1, 9),  # missing end
        date(2026, 1, 12),  # normal resumes
        date(2026, 1, 13),  # normal
    ]
    inj = MissingFeedInjection(start=date(2026, 1, 7), end=date(2026, 1, 9))
    ds = TelemetryDataset(seed=42, days_override=days, injections_override=[inj])

    run_dir = tmp_path / "missing_feed_run"
    score_res = await replay(ds, run_dir)

    assert len(score_res.matched) == 1
    assert all(prob.kind == "missing_feed" for prob in score_res.matched.values())
    assert all(prob.category == "data_quality" for prob in score_res.matched.values())
    assert all(v.value == "real_issue" for v in score_res.verdicts.values())
    assert len(score_res.misses) == 0


@pytest.mark.asyncio
async def test_late_file_window_raises_silence_and_recovers_when_file_arrives(
    tmp_path: Path,
) -> None:
    """A late-file window raises one silence incident scored real_issue, and feed is no longer silent once late file arrives."""
    days = [
        date(2026, 1, 5),  # normal
        date(2026, 1, 6),  # normal
        date(2026, 1, 7),  # late start (file delayed)
        date(2026, 1, 8),  # late (file delayed)
        date(2026, 1, 9),  # late end (file delayed, earlier late files arrive)
        date(2026, 1, 12),  # late files finish arriving
        date(2026, 1, 13),  # normal
    ]
    inj = LateArrivalInjection(start=date(2026, 1, 7), end=date(2026, 1, 9), delay_days=2)
    ds = TelemetryDataset(seed=42, days_override=days, injections_override=[inj])

    run_dir = tmp_path / "late_feed_run"
    score_res = await replay(ds, run_dir)

    assert len(score_res.matched) == 1
    assert all(prob.kind == "late_arrival" for prob in score_res.matched.values())
    assert all(prob.category == "data_quality" for prob in score_res.matched.values())
    assert all(v.value == "real_issue" for v in score_res.verdicts.values())
    assert len(score_res.misses) == 0

    # Also verify feed state is not silent at the end of the run
    settings = load_settings(
        RunMode.LOCAL,
        port=8765,
        env={
            "MLOPS_AGENTS_HOME": str(run_dir / "home"),
            "MLOPS_AGENTS_ACCESS_TOKEN": "test-token",
        },
    )
    app = create_app(settings)
    app.state.access_token = "test-token"
    async with app.router.lifespan_context(app):
        services = app.state.services
        projects = await services.workspace.list_projects()
        proj_id = projects[0].id
        states = await services.expectations.states(proj_id)
        feed_state = next((s for s in states if s.key.kind == "feed"), None)
        assert feed_state is not None
        assert feed_state.status != "silent", (
            f"Feed should not be silent after late arrival, got {feed_state.status}"
        )


@pytest.mark.asyncio
async def test_malformed_results_file_raises_parser_failure(tmp_path: Path) -> None:
    """A malformed results file gives a parser-failure incident."""
    days = [
        date(2026, 1, 5),  # normal
        date(2026, 1, 6),  # normal
        date(2026, 1, 7),  # malformed start
        date(2026, 1, 8),  # malformed end (2 days < parser_disable_after)
        date(2026, 1, 9),  # normal resumes
        date(2026, 1, 12),  # normal
        date(2026, 1, 13),  # normal
    ]
    inj = ParserFailureInjection(start=date(2026, 1, 7), end=date(2026, 1, 8))
    ds = TelemetryDataset(seed=42, days_override=days, injections_override=[inj])

    run_dir = tmp_path / "malformed_run"
    score_res = await replay(ds, run_dir)

    # Gives one incident per cause: the parser, and the check its results feed
    assert len(score_res.matched) == 2
    assert any(prob.kind == "parser_failure" for prob in score_res.matched.values())
    assert all(prob.category == "operational" for prob in score_res.matched.values())
    assert all(v.value == "real_issue" for v in score_res.verdicts.values())
    assert len(score_res.misses) == 0


@pytest.mark.asyncio
async def test_short_replay_through_replay(tmp_path: Path) -> None:
    """Runs a 15-day replay with 1 problem of each type."""
    days = telemetry_business_days(start_date=date(2026, 1, 5), count=15)
    # Day 0, 1 normal
    # Day 2..4: missing feed
    # Day 5..7 normal
    # Day 8..9: malformed results (2 days < parser_disable_after)
    # Day 10..14 normal
    injs = [
        MissingFeedInjection(start=days[2], end=days[4]),
        ParserFailureInjection(start=days[8], end=days[9]),
    ]
    ds = TelemetryDataset(seed=42, days_override=days, injections_override=injs)
    run_dir = tmp_path / "short_replay"
    score_res = await replay(ds, run_dir)

    assert len(score_res.matched) == 3
    matched_kinds = {prob.kind for prob in score_res.matched.values()}
    assert "missing_feed" in matched_kinds
    assert "parser_failure" in matched_kinds
    assert {prob.category for prob in score_res.matched.values()} == {"data_quality", "operational"}
    assert all(v.value == "real_issue" for v in score_res.verdicts.values())
    assert len(score_res.misses) == 0
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "report.json").is_file()


@pytest.mark.asyncio
async def test_full_scenario_replay(tmp_path: Path) -> None:
    """Full 60-day scenario run asserts 0 not_an_issue incidents and all 5 problems matched."""
    ds = TelemetryDataset(seed=42)
    run_dir = tmp_path / "full_scenario_run"
    score_res = await replay(ds, run_dir)

    assert len(score_res.misses) == 0, f"Expected 0 misses, got {score_res.misses}"
    assert len(score_res.matched) == 6, (
        f"Expected exactly 6 matched incidents (1 per feed problem, 2 for parser failure), got {len(score_res.matched)}"
    )

    # All 5 problems in the scenario must be matched
    matched_problems = set(score_res.matched.values())
    assert len(matched_problems) == 5, (
        f"Expected all 5 scenario problems to match, matched {len(matched_problems)}"
    )

    # No not_an_issue anywhere across the 60-day run
    assert all(v.value != "not_an_issue" for v in score_res.verdicts.values()), (
        f"Found not_an_issue incidents: {score_res.verdicts}"
    )
    assert all(v.value == "real_issue" for v in score_res.verdicts.values()), (
        f"Expected all verdicts to be real_issue, got {score_res.verdicts}"
    )
