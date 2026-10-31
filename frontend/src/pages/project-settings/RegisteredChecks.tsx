import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Badge } from "@/components/ui/badge";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import { formatCadence } from "@/pages/agents/proposals/formatCadence";
import { CATEGORY_LABELS } from "@/pages/incidents/labels";
import { checksKey } from "./checksKey";

type Check = components["schemas"]["CheckView"];

// Exhaustive over the generated union, so a status the backend adds fails to compile here.
const CHECK_STATUS_LABELS: Record<Check["status"], string> = {
  pending_pr: "Waiting for its pull request",
  active: "Active",
  retired: "Retired",
};

export function RegisteredChecks({ projectId }: { projectId: string }) {
  const checks = useQuery({
    queryKey: checksKey(projectId),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/checks", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
  // The PR observer ends the task, then activates or retires the checks of that PR.
  useInvalidateOnEvents(projectId, checksKey(projectId), [
    "check.registered",
    "task.ended",
    "pr.merged_by_agent",
  ]);
  return (
    <div className="space-y-3">
      <h3 className="text-base font-semibold">Registered checks</h3>
      <QueryStatus query={checks} loading="Loading the registered checks..." />
      {checks.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Agents register checks through a pull request, and a check starts running once that pull
          request is merged. None is registered yet.
        </p>
      )}
      {checks.data && checks.data.length > 0 && (
        <ul className="space-y-3">
          {checks.data.map((check) => (
            <CheckRow key={check.id} check={check} />
          ))}
        </ul>
      )}
    </div>
  );
}

function resultsSource(location: Check["results_location"]): string {
  if ("path_pattern" in location) return `${location.connection}: ${location.path_pattern}`;
  const rule =
    location.threshold != null && location.fires_when
      ? `, fires when ${location.fires_when} ${String(location.threshold)}`
      : "";
  return `MLflow metric ${location.metric}${rule}`;
}

function CheckRow({ check }: { check: Check }) {
  return (
    <li
      aria-label={`Check ${check.check_id}`}
      className={`space-y-2 rounded-md border p-3 text-sm ${check.status === "retired" ? "opacity-60" : ""}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{check.check_id}</span>
        <Badge variant={check.status === "retired" ? "outline" : "secondary"}>
          {CHECK_STATUS_LABELS[check.status]}
        </Badge>
      </div>
      <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">Category</dt>
          <dd>{CATEGORY_LABELS[check.category]}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Cadence</dt>
          <dd>{formatCadence(check.cadence)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Time zone</dt>
          <dd>{check.timezone}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Pull request</dt>
          <dd>{`#${String(check.pr_number)}`}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Registered by</dt>
          <dd>{check.agent}</dd>
        </div>
        {check.version && (
          <div>
            <dt className="text-muted-foreground">Version</dt>
            <dd>{check.version}</dd>
          </div>
        )}
        <div className="sm:col-span-2">
          <dt className="text-muted-foreground">Results read from</dt>
          <dd className="truncate" title={resultsSource(check.results_location)}>
            {resultsSource(check.results_location)}
          </dd>
        </div>
      </dl>
    </li>
  );
}
