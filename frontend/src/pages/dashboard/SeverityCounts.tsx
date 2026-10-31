import { cn } from "@/lib/utils";
import type { Dashboard } from "./useDashboard";

/** The severity names and their order are the API's; the page only lays them out. */
export function SeverityCounts({ counts }: { counts: Dashboard["incidents_by_severity"] }) {
  return (
    <section aria-labelledby="severity-title" className="space-y-3">
      <h2 id="severity-title" className="text-lg font-semibold">
        Incidents
      </h2>
      <ul
        aria-label="Active incidents by severity"
        className="grid grid-cols-2 gap-3 sm:grid-cols-4"
      >
        {Object.entries(counts).map(([severity, count]) => (
          <li
            key={severity}
            className={cn(
              "flex items-baseline justify-between rounded-lg border p-3",
              count > 0 ? "border-destructive/50 bg-destructive/5" : "text-muted-foreground",
            )}
          >
            <span className="text-sm capitalize">{severity}</span>
            <span className="text-2xl font-semibold tabular-nums">{count}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
