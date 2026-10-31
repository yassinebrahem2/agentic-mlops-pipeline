import { AlertCircle, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { Age, CategoryText } from "./IncidentBadges";
import { useIncidentSignals } from "./useIncidents";

export function Signals({ projectId, incidentId }: { projectId: string; incidentId: string }) {
  const query = useIncidentSignals(projectId, incidentId);
  return (
    <section aria-labelledby="signals-heading" className="space-y-3">
      <h2 id="signals-heading" className="text-lg font-semibold tracking-tight">
        Signals
      </h2>
      {query.isLoading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading signals...
        </div>
      )}
      {query.error && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading signals</AlertTitle>
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
      )}
      {query.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">No signals are attached.</p>
      )}
      <ul className="space-y-2">
        {query.data?.map((signal) => (
          <li
            key={signal.id}
            data-testid={`signal-${signal.id}`}
            className="space-y-1 rounded-md border bg-card p-3 text-sm"
          >
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="font-medium">{signal.subject}</span>
              <Badge variant="secondary">{signal.kind}</Badge>
              {signal.cleared && <Badge variant="outline">Cleared</Badge>}
              <span className="text-xs">
                <CategoryText category={signal.category} subtype={null} />
              </span>
              <span className="ml-auto text-xs text-muted-foreground">
                <Age iso={signal.ran_at} />
              </span>
            </div>
            <dl className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              {signal.source_ref && (
                <div className="flex gap-1">
                  <dt>Source</dt>
                  <dd className="font-mono text-foreground">{signal.source_ref}</dd>
                </div>
              )}
              {signal.result_id && (
                <div className="flex gap-1">
                  <dt>Result</dt>
                  <dd className="font-mono text-foreground">{signal.result_id}</dd>
                </div>
              )}
            </dl>
            {/* Shown as text: a detail can carry anything a check or a source reported. */}
            <pre className="max-h-48 overflow-auto rounded bg-muted p-2 text-xs whitespace-pre-wrap break-words">
              {JSON.stringify(signal.detail, null, 2)}
            </pre>
          </li>
        ))}
      </ul>
    </section>
  );
}
