/**
 * Formats a duration in seconds for UI displays.
 * null/undefined -> "–" (en-dash \u2013)
 * < 60s          -> "X s"
 * 60s .. 3599s   -> "X min Y s"
 * >= 3600s       -> "X h Y min"
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds) || seconds < 0) {
    return "–";
  }
  const total = Math.floor(seconds);
  if (total < 60) {
    return `${String(total)} s`;
  }
  if (total < 3600) {
    const mins = Math.floor(total / 60);
    const secs = total % 60;
    return `${String(mins)} min ${String(secs)} s`;
  }
  const hours = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  return `${String(hours)} h ${String(mins)} min`;
}

/**
 * Formats an ISO timestamp using toLocaleString.
 * Returns "–" if null, undefined, or invalid.
 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) {
    return "–";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "–";
  }
  return date.toLocaleString();
}

/**
 * Returns the first 7 characters of a git SHA or passes null/undefined through.
 */
export function shortCommit(sha: string | null | undefined): string | null | undefined {
  if (sha === null || sha === undefined) {
    return sha;
  }
  return sha.slice(0, 7);
}

const metricFormatter = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 3 });

/**
 * Formats a metric or numeric result for UI display:
 * - 3 significant digits for decimals (e.g. 0.854, 12.3, 0.00123)
 * - integers as integers with grouping (e.g. 1,234, 0, -5)
 * - null/undefined/NaN/non-finite -> "–" (en-dash \u2013)
 */
export function formatMetric(val: number | string | undefined | null): string {
  if (val === null || val === undefined) {
    return "–";
  }
  if (typeof val === "string" && val.trim() === "") {
    return "–";
  }
  const num = typeof val === "number" ? val : Number(val);
  if (!Number.isFinite(num)) {
    return "–";
  }
  if (Number.isInteger(num)) {
    return num.toLocaleString("en-US");
  }
  return metricFormatter.format(num);
}
