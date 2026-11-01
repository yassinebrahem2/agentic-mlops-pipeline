"""The benchmark report: writes report.md and report.json, and the report CLI."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from mlops_agents.stats.contract import STAT_METRICS, StatGroup, StatsView, StatValue

from benchmarks.client import BenchClient
from benchmarks.scorer import Score

# What each mode shows: the per-mode group lists live in one constant.
MODE_GROUPS: Final[Mapping[Literal["replay", "agents"], tuple[StatGroup, ...]]] = {
    "replay": ("detection", "speed", "noise", "diagnosis"),
    "agents": ("detection", "speed", "noise", "diagnosis", "effort", "cost"),
}

REPORT_GROUPS: Final[Mapping[Literal["replay", "agents"], tuple[StatGroup, ...]]] = MODE_GROUPS

# Metrics omitted in agents mode because simulated time differs from real time.
_AGENTS_OMITTED_METRICS: Final[frozenset[str]] = frozenset(
    {"alerts_per_week", "time_to_resolution_s"}
)


@dataclass(frozen=True)
class Pause:
    reason: Literal["ready_wait", "model_stall"]
    start: datetime
    end: datetime


@dataclass(frozen=True)
class RunInfo:
    mode: Literal["replay", "agents"]
    dataset: str
    scenario: str
    seed: int | None
    app_commit: str
    started: datetime
    ended: datetime
    provider: str | None
    model: str | None
    seconds_per_day: float | None
    pauses: Sequence[Pause]
    day_gaps: Sequence[date]


def _format_stat_value(stat: StatValue) -> str:
    if stat.collecting:
        return f"collecting (n={stat.n})"
    if stat.value is None:
        return f"not available (n={stat.n})"
    if isinstance(stat.value, float) and stat.value.is_integer():
        val_str = f"{stat.value:.1f}"
    else:
        val_str = f"{stat.value:.3f}"
    return f"{val_str} (n={stat.n})"


def _format_stat_value_short(stat: StatValue | None) -> str:
    if stat is None:
        return "N/A"
    if stat.collecting:
        return "collecting"
    if stat.value is None:
        return "N/A"
    if isinstance(stat.value, float) and stat.value.is_integer():
        return f"{stat.value:.1f}"
    return f"{stat.value:.3f}"


def _metric_label(metric: str, mode: Literal["replay", "agents"]) -> str:
    if metric == "detection_delay_s":
        return "pickup delay: first firing result to incident"
    if metric == "bad_data_vs_drift_correct" and mode == "replay":
        return "category of the check that fired"
    return metric


def _render_stats_section(
    stats_view: StatsView,
    mode: Literal["replay", "agents"],
    header_level: int = 3,
) -> str:
    lines: list[str] = []
    prefix = "#" * header_level
    measured_groups = set(MODE_GROUPS.get(mode, ()))

    for group_name in STAT_METRICS:
        title = group_name.replace("_", " ").title()
        lines.append(f"{prefix} {title}")
        if group_name not in measured_groups:
            lines.append("not measured in this mode\n")
            continue

        group_stats = stats_view.groups.get(group_name, {})
        metric_lines: list[str] = []
        for metric in STAT_METRICS[group_name]:
            if mode == "agents" and metric in _AGENTS_OMITTED_METRICS:
                continue
            label = _metric_label(metric, mode)
            stat = group_stats.get(metric)
            if stat is None:
                metric_lines.append(f"- **{label}:** not available (n=0)")
            else:
                metric_lines.append(f"- **{label}:** {_format_stat_value(stat)}")

        if metric_lines:
            lines.extend(metric_lines)
        else:
            lines.append("not measured in this mode")
        lines.append("")

    return "\n".join(lines)


def report_title(info: RunInfo) -> str:
    """Every report names its mode in its title, and an agents run with a stall says so."""
    if info.mode == "replay":
        return "Benchmark Report: Replay Mode"
    has_stall = any(p.reason == "model_stall" for p in info.pauses)
    if has_stall:
        return "Benchmark Report: Agents Mode (stalled)"
    return "Benchmark Report: Agents Mode"


def _render_markdown(
    info: RunInfo,
    score: Score,
    problem_counts: Mapping[str, int],
    overall_stats: StatsView,
    category_stats: Mapping[str, StatsView],
    usage_data: Mapping[str, Any] | None,
) -> str:
    lines: list[str] = []

    # Title
    title = report_title(info)
    lines.append(f"# {title}\n")

    # Pauses (prominently listed for agents mode)
    if info.mode == "agents":
        lines.append("### Pauses")
        if info.pauses:
            for p in info.pauses:
                duration_s = (p.end - p.start).total_seconds()
                reason_str = "Readiness wait" if p.reason == "ready_wait" else "Model stall"
                lines.append(
                    f"- **{reason_str}:** {p.start.isoformat()} to {p.end.isoformat()} ({duration_s:.1f}s)"
                )
        else:
            lines.append("- **Pauses:** none")
        lines.append("")

    # Run Information
    duration = (info.ended - info.started).total_seconds()
    lines.append("## Run Information\n")
    lines.append(f"- **Mode:** {info.mode}")
    lines.append(f"- **Dataset:** {info.dataset}")
    lines.append(f"- **Scenario:** {info.scenario}")
    lines.append(f"- **Seed:** {info.seed if info.seed is not None else 'none'}")
    lines.append(f"- **App commit:** {info.app_commit}")
    lines.append(f"- **Started:** {info.started.isoformat()}")
    lines.append(f"- **Ended:** {info.ended.isoformat()}")
    lines.append(f"- **Duration:** {duration:.1f}s")
    if info.mode == "agents":
        lines.append(f"- **Provider:** {info.provider or 'none'}")
        lines.append(f"- **Model:** {info.model or 'none'}")
    if info.seconds_per_day is not None:
        lines.append(f"- **Seconds per day:** {info.seconds_per_day}")
    if info.day_gaps:
        gaps_str = ", ".join(d.isoformat() for d in info.day_gaps)
        lines.append(f"- **Day gaps:** {gaps_str}")
    lines.append("")

    # Problem Counts
    lines.append("## Problem Counts\n")
    if problem_counts:
        for cat, cnt in sorted(problem_counts.items()):
            lines.append(f"- **{cat}:** {cnt}")
    else:
        lines.append("No injected problems.")
    lines.append("")

    # Overall Stats
    lines.append("## Overall Stats\n")
    lines.append(_render_stats_section(overall_stats, info.mode, header_level=3))

    # Category Stats
    if category_stats:
        lines.append("## Stats by Category\n")
        for cat, cat_view in sorted(category_stats.items()):
            lines.append(f"### Category: {cat}\n")
            lines.append(_render_stats_section(cat_view, info.mode, header_level=4))

    # Usage and Planted Flaws for Agents Mode
    if info.mode == "agents":
        lines.append("## Usage\n")
        lines.append(f"- **Run Duration:** {duration:.1f}s")

        total_p_tok = 0
        total_c_tok = 0
        total_cost = 0.0
        has_cost = True
        total_reqs: int | None = None
        agent_rows: dict[str, dict[str, Any]] = {}

        if usage_data and "rows" in usage_data and isinstance(usage_data["rows"], list):
            raw_rows: list[object] = cast(list[object], usage_data["rows"])
            for row_obj in raw_rows:
                if isinstance(row_obj, dict):
                    row: dict[str, Any] = cast(dict[str, Any], row_obj)
                    agent = str(row.get("agent", "unknown"))
                    model = str(row.get("model_address", ""))
                    p_tok = int(cast(int, row.get("prompt_tokens", 0)))
                    c_tok = int(cast(int, row.get("completion_tokens", 0)))
                    cost_val: object = row.get("cost_usd")

                    raw_reqs: object = row.get("requests")
                    reqs_int: int | None = (
                        int(cast(int, raw_reqs)) if raw_reqs is not None else None
                    )
                    if reqs_int is not None:
                        total_reqs = (total_reqs or 0) + reqs_int

                    total_p_tok += p_tok
                    total_c_tok += c_tok
                    if cost_val is not None:
                        total_cost += float(cast(float, cost_val))
                    else:
                        has_cost = False

                    if agent not in agent_rows:
                        agent_rows[agent] = {
                            "agent": agent,
                            "model": model,
                            "requests": reqs_int,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "cost": 0.0 if cost_val is not None else None,
                        }
                    else:
                        if reqs_int is not None:
                            curr_reqs = cast(int | None, agent_rows[agent]["requests"])
                            agent_rows[agent]["requests"] = (
                                curr_reqs + reqs_int if curr_reqs is not None else reqs_int
                            )
                    agent_rows[agent]["prompt_tokens"] += p_tok
                    agent_rows[agent]["completion_tokens"] += c_tok
                    agent_rows[agent]["total_tokens"] += p_tok + c_tok
                    if cost_val is not None and agent_rows[agent]["cost"] is not None:
                        agent_rows[agent]["cost"] += float(cast(float, cost_val))

        total_tokens = total_p_tok + total_c_tok
        if total_reqs is not None:
            lines.append(f"- **Total Requests:** {total_reqs}")
        else:
            lines.append("- **Total Requests:** not available")
        lines.append(
            f"- **Total Tokens:** {total_tokens:,} (Prompt: {total_p_tok:,}, Completion: {total_c_tok:,})"
        )
        cost_str = f"${total_cost:.4f}" if has_cost else "not available"
        lines.append(f"- **Total Cost:** {cost_str}\n")

        if agent_rows:
            lines.append("### Usage by Agent\n")
            lines.append("| Agent | Model | Requests | Tokens | Cost |")
            lines.append("|---|---|---|---|---|")
            for ag in sorted(agent_rows.keys()):
                d = agent_rows[ag]
                reqs_cell = str(d["requests"]) if d["requests"] is not None else "N/A"
                c_str = f"${d['cost']:.4f}" if d["cost"] is not None else "N/A"
                lines.append(
                    f"| {d['agent']} | {d['model']} | {reqs_cell} | {d['total_tokens']:,} | {c_str} |"
                )
            lines.append("")

        # Planted flaws checklist (deduplicated)
        lines.append("## Planted Flaws\n")
        unique_matched = sorted(
            set(score.matched.values()),
            key=lambda p: (p.start, str(p.category), p.kind),
        )
        unique_misses = sorted(
            set(score.misses) - set(score.matched.values()),
            key=lambda p: (p.start, str(p.category), p.kind),
        )
        for p in unique_matched:
            cols = list(p.columns)
            lines.append(f"- [x] {p.kind} ({p.category}, {p.start} to {p.end}, columns: {cols})")
        for p in unique_misses:
            cols = list(p.columns)
            lines.append(f"- [ ] {p.kind} ({p.category}, {p.start} to {p.end}, columns: {cols})")
        lines.append("")

    return "\n".join(lines)


def _write_report_files_sync(
    run_dir: Path,
    md_content: str,
    json_content: str,
) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "report.md"
    md_path.write_text(md_content, encoding="utf-8")
    json_path = run_dir / "report.json"
    json_path.write_text(json_content, encoding="utf-8")
    return md_path, json_path


async def write_report(
    client: BenchClient,
    project_id: str,
    run_dir: Path,
    info: RunInfo,
    score: Score,
) -> tuple[Path, Path]:
    """Reads /stats and /usage and writes report.md and report.json in run_dir."""
    # 1. Fetch overall stats
    raw_overall = await client.get(f"/projects/{project_id}/stats", params={"window": "all"})
    if isinstance(raw_overall, StatsView):
        overall_stats = raw_overall
    elif isinstance(raw_overall, dict):
        overall_stats = StatsView.model_validate(raw_overall)
    else:
        raise RuntimeError(f"Unexpected response for overall stats: {raw_overall}")

    # 2. Extract unique problems and counts per category (deduplicated)
    unique_matched = set(score.matched.values())
    unique_misses = set(score.misses) - unique_matched
    all_unique_problems = list(unique_matched | unique_misses)
    problem_counts: dict[str, int] = {}
    for p in all_unique_problems:
        cat_key = str(p.category)
        problem_counts[cat_key] = problem_counts.get(cat_key, 0) + 1

    categories = sorted(problem_counts.keys())

    # 3. Fetch category stats
    category_stats: dict[str, StatsView] = {}
    for cat in categories:
        raw_cat = await client.get(
            f"/projects/{project_id}/stats",
            params={"window": "all", "category": cat},
        )
        if isinstance(raw_cat, StatsView):
            category_stats[cat] = raw_cat
        elif isinstance(raw_cat, dict):
            category_stats[cat] = StatsView.model_validate(raw_cat)

    # 4. Fetch usage in agents mode
    usage_data: dict[str, Any] | None = None
    if info.mode == "agents":
        since_date = info.started.date().isoformat()
        raw_usage = await client.get("/usage", params={"since": since_date})
        if isinstance(raw_usage, dict):
            usage_data = cast(dict[str, Any], raw_usage)
        elif hasattr(raw_usage, "model_dump") and callable(raw_usage.model_dump):
            usage_data = cast(dict[str, Any], raw_usage.model_dump(mode="json"))

    # 5. Build report.md
    md_content = _render_markdown(
        info=info,
        score=score,
        problem_counts=problem_counts,
        overall_stats=overall_stats,
        category_stats=category_stats,
        usage_data=usage_data,
    )

    # 6. Build report.json
    rep_title = report_title(info)
    json_data: dict[str, Any] = {
        "title": rep_title,
        "info": {
            "title": rep_title,
            "mode": info.mode,
            "dataset": info.dataset,
            "scenario": info.scenario,
            "seed": info.seed,
            "app_commit": info.app_commit,
            "started": info.started.isoformat(),
            "ended": info.ended.isoformat(),
            "provider": info.provider,
            "model": info.model,
            "seconds_per_day": info.seconds_per_day,
            "pauses": [
                {
                    "reason": p.reason,
                    "start": p.start.isoformat(),
                    "end": p.end.isoformat(),
                    "duration_s": (p.end - p.start).total_seconds(),
                }
                for p in info.pauses
            ],
            "day_gaps": [d.isoformat() for d in info.day_gaps],
        },
        "score": {
            "matched": {
                inc_id: {
                    "kind": p.kind,
                    "category": str(p.category),
                    "start": p.start.isoformat(),
                    "end": p.end.isoformat(),
                    "columns": list(p.columns),
                }
                for inc_id, p in score.matched.items()
            },
            "verdicts": {
                inc_id: v.model_dump(mode="json", exclude_none=True)
                for inc_id, v in score.verdicts.items()
            },
            "misses": [
                {
                    "kind": p.kind,
                    "category": str(p.category),
                    "start": p.start.isoformat(),
                    "end": p.end.isoformat(),
                    "columns": list(p.columns),
                }
                for p in score.misses
            ],
            "silent": {res_id: str(v) for res_id, v in score.silent.items()},
            "excluded": list(score.excluded),
        },
        "problem_counts": problem_counts,
        "overall_stats": overall_stats.model_dump(mode="json"),
        "category_stats": {
            cat: cview.model_dump(mode="json") for cat, cview in category_stats.items()
        },
        "usage": usage_data,
    }

    return _write_report_files_sync(
        run_dir,
        md_content,
        json.dumps(json_data, indent=2),
    )


def format_comparison_table(report_json_paths: Sequence[Path]) -> str:
    """Consolidates several report.json files into one markdown comparison table."""
    headers = [
        "Run",
        "Mode",
        "Dataset",
        "Scenario",
        "Caught",
        "Precision",
        "Misses",
        "Pickup Delay",
        "Tokens",
        "Duration",
    ]
    rows: list[list[str]] = []

    for path in report_json_paths:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
        data: dict[str, Any] = cast(dict[str, Any], raw_data) if isinstance(raw_data, dict) else {}
        info: dict[str, Any] = cast(dict[str, Any], data.get("info", {}))
        score_data: dict[str, Any] = cast(dict[str, Any], data.get("score", {}))
        overall: dict[str, Any] = cast(dict[str, Any], data.get("overall_stats", {}))
        groups: dict[str, Any] = cast(dict[str, Any], overall.get("groups", {}))

        name = path.parent.name if path.name == "report.json" else path.name
        mode = str(info.get("mode", ""))
        dataset = str(info.get("dataset", ""))
        scenario = str(info.get("scenario", ""))

        # Deduplicate matched and misses in score_data
        matched_map = cast(dict[str, Any], score_data.get("matched", {}))
        unique_matched_keys: set[tuple[str, str, str, str, tuple[str | None, ...]]] = set()
        for p_val in matched_map.values():
            if isinstance(p_val, dict):
                p_dict = cast(dict[str, Any], p_val)
                cols_raw = p_dict.get("columns", [])
                cols: tuple[str | None, ...] = (
                    tuple(cast(list[str | None], cols_raw)) if isinstance(cols_raw, list) else ()
                )
                unique_matched_keys.add(
                    (
                        str(p_dict.get("kind", "")),
                        str(p_dict.get("category", "")),
                        str(p_dict.get("start", "")),
                        str(p_dict.get("end", "")),
                        cols,
                    )
                )

        misses_raw = score_data.get("misses", [])
        misses_list = cast(list[Any], misses_raw) if isinstance(misses_raw, list) else []
        unique_missed_keys: set[tuple[str, str, str, str, tuple[str | None, ...]]] = set()
        for p_val in misses_list:
            if isinstance(p_val, dict):
                p_dict = cast(dict[str, Any], p_val)
                cols_raw = p_dict.get("columns", [])
                cols = tuple(cast(list[str | None], cols_raw)) if isinstance(cols_raw, list) else ()
                unique_missed_keys.add(
                    (
                        str(p_dict.get("kind", "")),
                        str(p_dict.get("category", "")),
                        str(p_dict.get("start", "")),
                        str(p_dict.get("end", "")),
                        cols,
                    )
                )

        unique_missed_only = unique_missed_keys - unique_matched_keys
        matched_cnt = len(unique_matched_keys)
        total_problems = matched_cnt + len(unique_missed_only)
        caught_str = f"{matched_cnt}/{total_problems}"

        det: dict[str, Any] = cast(dict[str, Any], groups.get("detection", {}))
        prec_val = det.get("precision", {})
        misses_val = det.get("misses", {})
        prec_stat = StatValue.model_validate(prec_val) if prec_val else None
        misses_stat = StatValue.model_validate(misses_val) if misses_val else None

        speed: dict[str, Any] = cast(dict[str, Any], groups.get("speed", {}))
        delay_val = speed.get("detection_delay_s", {})
        delay_stat = StatValue.model_validate(delay_val) if delay_val else None

        prec_str = _format_stat_value_short(prec_stat)
        misses_str = _format_stat_value_short(misses_stat)
        delay_str = (
            f"{_format_stat_value_short(delay_stat)}s"
            if delay_stat and delay_stat.value is not None
            else _format_stat_value_short(delay_stat)
        )

        # Tokens
        raw_usage = data.get("usage")
        if (
            raw_usage
            and isinstance(raw_usage, dict)
            and "rows" in raw_usage
            and isinstance(raw_usage["rows"], list)
        ):
            raw_usage_rows: list[object] = cast(list[object], raw_usage["rows"])
            tok_sum = 0
            for r in raw_usage_rows:
                if isinstance(r, dict):
                    row_dict: dict[str, Any] = cast(dict[str, Any], r)
                    p = int(cast(int, row_dict.get("prompt_tokens", 0)))
                    c = int(cast(int, row_dict.get("completion_tokens", 0)))
                    tok_sum += p + c
            tokens_str = f"{tok_sum:,}"
        else:
            tokens_str = "N/A"

        # Duration
        try:
            started = datetime.fromisoformat(str(info.get("started", "")))
            ended = datetime.fromisoformat(str(info.get("ended", "")))
            dur_s = (ended - started).total_seconds()
            dur_str = f"{dur_s:.1f}s"
        except Exception:
            dur_str = "N/A"

        rows.append(
            [
                name,
                mode,
                dataset,
                scenario,
                caught_str,
                prec_str,
                misses_str,
                delay_str,
                tokens_str,
                dur_str,
            ]
        )

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(val))

    header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"
    sep_line = "| " + " | ".join("-" * col_widths[i] for i in range(len(headers))) + " |"
    table_lines = [header_line, sep_line]
    for row in rows:
        row_line = "| " + " | ".join(v.ljust(col_widths[i]) for i, v in enumerate(row)) + " |"
        table_lines.append(row_line)

    return "\n".join(table_lines)


def register_report_cli(parser: argparse.ArgumentParser) -> None:
    """Registers CLI arguments for `bench report`."""
    parser.add_argument(
        "run_dirs",
        nargs="+",
        type=Path,
        help="One or more run directories containing report.json",
    )


def run_report_cli(args: argparse.Namespace) -> int:
    """CLI handler for `bench report <run_dir>...`."""
    raw_dirs: list[Path] = getattr(args, "run_dirs", [])
    report_paths: list[Path] = []

    for d in raw_dirs:
        path = Path(d)
        target = path / "report.json" if path.is_dir() else path
        if not target.exists():
            sys.stderr.write(f"Error: {target} does not exist\n")
            return 1
        report_paths.append(target)

    table = format_comparison_table(report_paths)
    sys.stdout.write(table + "\n")
    return 0


__all__ = [
    "MODE_GROUPS",
    "REPORT_GROUPS",
    "Pause",
    "RunInfo",
    "format_comparison_table",
    "register_report_cli",
    "report_title",
    "run_report_cli",
    "write_report",
]
