import { useMemo } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { ActionError } from "./ActionError";
import {
  Age,
  CategoryText,
  IncidentLink,
  SeverityBadge,
  StatusBadge,
  VerdictSummary,
} from "./IncidentBadges";
import { VerdictButtons } from "./VerdictButtons";
import type { IncidentView } from "./labels";
import { useAcknowledge } from "./useIncidentActions";
import { useActiveIncidents, useRefreshOnIncidentEvents } from "./useIncidents";

function IncidentRow({ projectId, incident }: { projectId: string; incident: IncidentView }) {
  const acknowledge = useAcknowledge({ projectId, incidentId: incident.id });
  return (
    <li
      data-testid={`incident-row-${incident.id}`}
      className="space-y-2 rounded-lg border border-border/80 bg-card p-3 shadow-xs"
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <SeverityBadge severity={incident.severity} />
        <StatusBadge status={incident.status} />
        <span className="text-xs">
          <CategoryText category={incident.category} subtype={incident.subtype} />
        </span>
        <span className="ml-auto text-xs text-muted-foreground">
          <Age iso={incident.detected_at} />
        </span>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="min-w-0 text-sm font-medium">
          <IncidentLink projectId={projectId} incidentId={incident.id}>
            {incident.cause ?? "Cause not known yet"}
          </IncidentLink>
        </p>
        {incident.acknowledged_at === null && (
          <Button
            size="sm"
            variant="secondary"
            disabled={acknowledge.isPending}
            onClick={() => {
              acknowledge.mutate();
            }}
          >
            Acknowledge
          </Button>
        )}
      </div>
      <ActionError error={acknowledge.error} />
      {incident.verdict ? (
        <VerdictSummary verdict={incident.verdict} />
      ) : (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">Give a verdict</summary>
          <div className="pt-2">
            <VerdictButtons projectId={projectId} incidentId={incident.id} />
          </div>
        </details>
      )}
    </li>
  );
}

export function IncidentList({ projectId }: { projectId: string }) {
  useRefreshOnIncidentEvents(projectId);
  const query = useActiveIncidents(projectId);
  const incidents = useMemo(() => {
    const seen = new Set<string>();
    // A refetch after a new incident shifts rows between pages; the first occurrence keeps the backend's order.
    return (query.data?.pages ?? [])
      .flatMap((page) => page.rows)
      .filter((row) => !seen.has(row.id) && seen.add(row.id));
  }, [query.data]);

  if (query.isLoading) {
    return (
      <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        Loading incidents...
      </div>
    );
  }

  if (query.error) {
    return (
      <Alert variant="destructive">
        <AlertCircle className="size-4" />
        <AlertTitle>Error loading incidents</AlertTitle>
        <AlertDescription className="space-y-2">
          <p>{errorText(query.error)}</p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              if (query.isFetchNextPageError) void query.fetchNextPage();
              else void query.refetch();
            }}
          >
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    );
  }

  if (incidents.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center">
        <p className="text-sm text-muted-foreground">No active incidents.</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <ul className="space-y-3">
        {incidents.map((incident) => (
          <IncidentRow key={incident.id} projectId={projectId} incident={incident} />
        ))}
      </ul>
      {query.hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            disabled={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
          >
            {query.isFetchingNextPage && <Loader2 className="size-4 animate-spin" />}
            Load more incidents
          </Button>
        </div>
      )}
    </div>
  );
}
