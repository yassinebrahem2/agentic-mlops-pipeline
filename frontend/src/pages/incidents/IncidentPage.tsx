import { Link, useParams } from "react-router";
import { AlertCircle, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ApiError, errorText } from "@/lib/api";
import { ActionError } from "./ActionError";
import { CategoryText, SeverityBadge, StatusBadge, VerdictSummary } from "./IncidentBadges";
import { IncidentFacts } from "./IncidentFacts";
import { incidentActions } from "./incidentActions";
import { Signals } from "./Signals";
import { Timeline } from "./Timeline";
import { VerdictButtons } from "./VerdictButtons";
import type { IncidentView } from "./labels";
import { useAcknowledge, useCloseIncident } from "./useIncidentActions";
import { useIncident, useRefreshOnIncidentEvents } from "./useIncidents";

export function IncidentPage() {
  const { projectId, incidentId } = useParams<{ projectId: string; incidentId: string }>();
  if (!projectId || !incidentId) return null;
  return <Incident projectId={projectId} incidentId={incidentId} />;
}

function BackLink({ projectId }: { projectId: string }) {
  return (
    <Link
      to={`/projects/${projectId}/inbox`}
      className="text-sm text-primary underline-offset-2 hover:underline"
    >
      Back to the Inbox
    </Link>
  );
}

function Actions({ projectId, incident }: { projectId: string; incident: IncidentView }) {
  const target = { projectId, incidentId: incident.id };
  const acknowledge = useAcknowledge(target);
  const close = useCloseIncident(target);
  const { canAcknowledge, canClose } = incidentActions(incident);
  if (!canAcknowledge && !canClose) return null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {canAcknowledge && (
          <Button
            variant="secondary"
            disabled={acknowledge.isPending}
            onClick={() => {
              acknowledge.mutate();
            }}
          >
            Acknowledge
          </Button>
        )}
        {canClose && (
          <Button
            variant="outline"
            disabled={close.isPending}
            onClick={() => {
              close.mutate();
            }}
          >
            Close incident
          </Button>
        )}
      </div>
      <ActionError error={acknowledge.error ?? close.error} />
    </div>
  );
}

function Incident({ projectId, incidentId }: { projectId: string; incidentId: string }) {
  useRefreshOnIncidentEvents(projectId);
  const query = useIncident(projectId, incidentId);

  if (query.isLoading) {
    return (
      <div className="container max-w-4xl py-6">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading incident...
        </div>
      </div>
    );
  }

  if (query.error instanceof ApiError && query.error.status === 404) {
    return (
      <div className="container max-w-4xl space-y-3 py-6">
        <h1 className="text-2xl font-bold tracking-tight">Incident not found</h1>
        <p className="text-sm text-muted-foreground">
          This project has no incident with the id {incidentId}.
        </p>
        <BackLink projectId={projectId} />
      </div>
    );
  }

  if (query.error || !query.data) {
    return (
      <div className="container max-w-4xl space-y-3 py-6">
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading the incident</AlertTitle>
          <AlertDescription className="space-y-2">
            <p>{errorText(query.error)}</p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void query.refetch();
              }}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
        <BackLink projectId={projectId} />
      </div>
    );
  }

  const incident = query.data;
  return (
    <div className="container max-w-4xl space-y-8 py-6">
      <div className="space-y-3">
        <BackLink projectId={projectId} />
        <div data-testid="incident-header" className="flex flex-wrap items-center gap-2">
          <SeverityBadge severity={incident.severity} />
          <StatusBadge status={incident.status} />
          <span className="text-sm">
            <CategoryText category={incident.category} subtype={incident.subtype} />
          </span>
          <span className="font-mono text-xs text-muted-foreground">{incident.id}</span>
        </div>
        <h1 className="text-2xl font-bold tracking-tight">
          {incident.cause ?? "Cause not known yet"}
        </h1>
        <Actions projectId={projectId} incident={incident} />
      </div>

      <IncidentFacts projectId={projectId} incident={incident} />

      <section aria-labelledby="verdict-heading" className="space-y-3">
        <h2 id="verdict-heading" className="text-lg font-semibold tracking-tight">
          Verdict
        </h2>
        {incident.verdict && (
          <div data-testid="incident-verdict" className="space-y-1 rounded-md border p-3 text-sm">
            <VerdictSummary verdict={incident.verdict} />
            {incident.verdict.note && <p>{incident.verdict.note}</p>}
          </div>
        )}
        <VerdictButtons projectId={projectId} incidentId={incident.id} />
      </section>

      <Signals projectId={projectId} incidentId={incident.id} />
      <Timeline projectId={projectId} incidentId={incident.id} />
    </div>
  );
}
