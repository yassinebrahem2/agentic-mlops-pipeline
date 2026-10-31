import type { components } from "@/api/schema";
import { formatDuration } from "@/lib/format";
import { formatUsd } from "@/pages/usage/format";

type StatsView = components["schemas"]["StatsView"];
export type StatsWindow = StatsView["window"];
export type ResultCategory = NonNullable<StatsView["category"]>;

export type MetricUnit = "seconds" | "percent" | "usd" | "count" | "ratio" | "number";

interface MetricLabel {
  label: string;
  unit: MetricUnit;
}

// Display only: the page lists whatever the API returns and falls back to the raw name.
const METRIC_LABELS: ReadonlyMap<string, MetricLabel> = new Map<string, MetricLabel>([
  ["precision", { label: "Precision", unit: "percent" }],
  ["caught_baseline_missed", { label: "Caught, team missed", unit: "count" }],
  ["false_alarms_avoided", { label: "False alarms avoided", unit: "count" }],
  ["misses", { label: "Misses", unit: "count" }],
  ["detection_delay_s", { label: "Detection delay", unit: "seconds" }],
  ["time_to_resolution_s", { label: "Time to resolution", unit: "seconds" }],
  ["alerts_per_week", { label: "Alerts per week", unit: "number" }],
  ["signals_per_incident", { label: "Signals per incident", unit: "number" }],
  ["bad_data_vs_drift_correct", { label: "Bad data versus drift right", unit: "percent" }],
  ["autonomous_share", { label: "Handled without a person", unit: "percent" }],
  ["approvals_requested", { label: "Approvals requested", unit: "count" }],
  ["acceptance_rate", { label: "Approvals accepted", unit: "percent" }],
  ["prs_opened", { label: "PRs opened", unit: "count" }],
  ["prs_merged", { label: "PRs merged", unit: "count" }],
  ["prs_closed", { label: "PRs closed", unit: "count" }],
  ["metric_before", { label: "Model metric before", unit: "number" }],
  ["metric_after", { label: "Model metric after", unit: "number" }],
  ["tokens_per_agent", { label: "Tokens per agent", unit: "count" }],
  ["cost_per_incident", { label: "Cost per incident", unit: "usd" }],
  ["failed_runs", { label: "Failed runs", unit: "count" }],
  ["tool_errors", { label: "Tool errors", unit: "count" }],
  ["sandbox_failures", { label: "Sandbox failures", unit: "count" }],
]);

const GROUP_LABELS: ReadonlyMap<string, string> = new Map<string, string>([
  ["detection", "Detection"],
  ["speed", "Speed"],
  ["noise", "Noise"],
  ["diagnosis", "Diagnosis"],
  ["effort", "Effort"],
  ["model_impact", "Model impact"],
  ["cost", "Cost"],
  ["reliability", "Reliability"],
]);

// Records over the schema's own unions, so tsc fails when the API gains or loses a value.
export const WINDOW_LABELS: Record<StatsWindow, string> = {
  "7d": "7 days",
  "30d": "30 days",
  all: "All time",
};

export const CATEGORY_LABELS: Record<ResultCategory, string> = {
  data_quality: "Data quality",
  drift: "Drift",
  model_performance: "Model performance",
  operational: "Operational",
};

export function metricLabel(name: string): MetricLabel {
  return METRIC_LABELS.get(name) ?? { label: name, unit: "number" };
}

export function groupLabel(name: string): string {
  return GROUP_LABELS.get(name) ?? name;
}

export function isStatsWindow(value: string): value is StatsWindow {
  return Object.hasOwn(WINDOW_LABELS, value);
}

export function isResultCategory(value: string): value is ResultCategory {
  return Object.hasOwn(CATEGORY_LABELS, value);
}

const PERCENT = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
const COUNT = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const DECIMAL = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function formatStat(value: number, unit: MetricUnit): string {
  switch (unit) {
    case "seconds":
      return formatDuration(value);
    case "percent":
      return PERCENT.format(value);
    case "usd":
      return formatUsd(value);
    case "count":
      return COUNT.format(value);
    case "ratio":
    case "number":
      return DECIMAL.format(value);
  }
}
