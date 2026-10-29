import { shortCommit } from "@/lib/format";
import type { Page_CiRunRow_, Page_RunRow_, RunRow, RunStatus } from "@/lib/useTracking";

export type RunListItem = {
  kind: "run" | "ci";
  sourceId: string;
  key: string;
  name: string;
  status: RunStatus;
  start: string | null;
  durationSeconds: number | null;
  detail: string | null;
  url: string | null;
};

function runDurationSeconds(run: RunRow): number | null {
  if (run.status === "running") {
    return null;
  }
  if (!run.start || !run.end) {
    return null;
  }
  const s = Date.parse(run.start);
  const e = Date.parse(run.end);
  if (Number.isNaN(s) || Number.isNaN(e)) {
    return null;
  }
  return Math.max(0, (e - s) / 1000);
}

function runDetail(run: RunRow): string | null {
  const commit = shortCommit(run.git_commit);
  const datasets = run.datasets.length > 0 ? run.datasets.join(", ") : null;
  if (commit && datasets) {
    return `${commit} (${datasets})`;
  }
  return commit ?? datasets ?? null;
}

export function mergeRuns(runs: Page_RunRow_, ci: Page_CiRunRow_): RunListItem[] {
  const runItems: RunListItem[] = runs.rows.map((r) => ({
    kind: "run",
    sourceId: r.source_id,
    key: r.key,
    name: r.experiment,
    status: r.status,
    start: r.start,
    durationSeconds: runDurationSeconds(r),
    detail: runDetail(r),
    url: null,
  }));

  const ciItems: RunListItem[] = ci.rows.map((c) => ({
    kind: "ci",
    sourceId: c.source_id,
    key: c.key,
    name: c.workflow,
    status: c.status,
    start: c.start,
    durationSeconds: c.duration_s,
    detail: shortCommit(c.commit) ?? null,
    url: c.url,
  }));

  let combined = [...runItems, ...ciItems];

  // When either page is truncated, cut at the later of the two pages' oldest start
  if (runs.truncated || ci.truncated) {
    const runStarts = runs.rows
      .map((r) => (r.start ? Date.parse(r.start) : NaN))
      .filter((t) => !Number.isNaN(t));
    const ciStarts = ci.rows.map((c) => Date.parse(c.start)).filter((t) => !Number.isNaN(t));

    const oldestRun = runStarts.length > 0 ? Math.min(...runStarts) : null;
    const oldestCi = ciStarts.length > 0 ? Math.min(...ciStarts) : null;

    let cutoff: number | null = null;
    if (oldestRun !== null && oldestCi !== null) {
      cutoff = Math.max(oldestRun, oldestCi);
    } else if (oldestRun !== null) {
      cutoff = oldestRun;
    } else if (oldestCi !== null) {
      cutoff = oldestCi;
    }

    if (cutoff !== null) {
      combined = combined.filter((item) => {
        if (!item.start) return false;
        const t = Date.parse(item.start);
        return !Number.isNaN(t) && t >= cutoff;
      });
    }
  }

  // Sort newest first by start (rows without start last)
  combined.sort((a, b) => {
    if (a.start === null && b.start === null) {
      return a.key.localeCompare(b.key);
    }
    if (a.start === null) return 1;
    if (b.start === null) return -1;

    const diff = Date.parse(b.start) - Date.parse(a.start);
    if (diff !== 0) return diff;
    return a.key.localeCompare(b.key);
  });

  return combined;
}
