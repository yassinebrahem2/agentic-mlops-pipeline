import type { HandoffView } from "@/lib/useHandoff";
import type { MetricPoint, ModelVersionRow } from "@/lib/useTracking";

export interface VersionRow {
  version: string;
  aliases: string[];
  created: string;
  runKey: string | null;
  metrics: Record<string, number>;
}

export function buildVersionRows(
  versions: ModelVersionRow[],
  metrics: Record<string, MetricPoint[]>,
): VersionRow[] {
  const sorted = [...versions].sort((a, b) => {
    const timeA = Date.parse(a.created);
    const timeB = Date.parse(b.created);
    if (Number.isNaN(timeA) || Number.isNaN(timeB)) return 0;
    return timeB - timeA;
  });

  return sorted.map((v) => {
    const rowMetrics: Record<string, number> = {};
    if (v.run_key) {
      const runMetrics = metrics[v.run_key];
      if (runMetrics) {
        for (const m of runMetrics) {
          rowMetrics[m.metric] = m.value;
        }
      }
    }
    return {
      version: v.version,
      aliases: v.aliases,
      created: v.created,
      runKey: v.run_key,
      metrics: rowMetrics,
    };
  });
}

export function metricNames(rows: VersionRow[]): string[] {
  const set = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row.metrics)) {
      set.add(key);
    }
  }
  return Array.from(set).sort((a, b) => a.localeCompare(b));
}

export function primaryMetric(handoff: HandoffView | null, modelName: string): string | null {
  if (!handoff) {
    return null;
  }
  const spec = handoff.payload.models.find((m) => m.name === modelName);
  return spec ? spec.profile.primary_metric : null;
}

export function formatMetric(val: number | undefined | null): string {
  if (val === undefined || val === null || Number.isNaN(val)) {
    return "–";
  }
  if (!Number.isFinite(val)) {
    return "–";
  }
  return val.toPrecision(4);
}
