import { useMemo } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { Age } from "./IncidentBadges";
import { eventLabel } from "./labels";
import { useIncidentTimeline } from "./useIncidents";

// Payloads are scrubbed by the server; React renders them as text.
function Payload({ value }: { value: Record<string, unknown> }) {
  if (Object.keys(value).length === 0) return null;
  return (
    <details className="mt-1 text-xs">
      <summary className="cursor-pointer text-muted-foreground">Details</summary>
      <pre className="mt-1 max-h-48 overflow-auto rounded bg-muted p-2 whitespace-pre-wrap break-words">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}

export function Timeline({ projectId, incidentId }: { projectId: string; incidentId: string }) {
  const query = useIncidentTimeline(projectId, incidentId);
  const events = useMemo(() => {
    const seen = new Set<string>();
    // A refetch after a new event shifts older ones down a page, so one can load twice.
    return (query.data?.pages ?? [])
      .flat()
      .filter((event) => !seen.has(event.id) && seen.add(event.id));
  }, [query.data]);

  return (
    <section aria-labelledby="timeline-heading" className="space-y-3">
      <h2 id="timeline-heading" className="text-lg font-semibold tracking-tight">
        Timeline
      </h2>
      {query.isLoading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading timeline...
        </div>
      )}
      {query.error && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading the timeline</AlertTitle>
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
      )}
      {!query.isLoading && !query.error && events.length === 0 && (
        <p className="text-sm text-muted-foreground">Nothing has happened to this incident yet.</p>
      )}
      {events.length > 0 && (
        <ol data-testid="incident-timeline" className="space-y-2">
          {events.map((event) => (
            <li key={event.id} className="rounded-md border bg-card p-3 text-sm">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="font-medium">{eventLabel(event.type)}</span>
                <span className="text-xs text-muted-foreground">{event.source}</span>
                <span className="ml-auto text-xs text-muted-foreground">
                  <Age iso={event.ts} />
                </span>
              </div>
              <Payload value={event.payload} />
            </li>
          ))}
        </ol>
      )}
      {query.hasNextPage && (
        <Button
          variant="outline"
          disabled={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          {query.isFetchingNextPage && <Loader2 className="size-4 animate-spin" />}
          Load older events
        </Button>
      )}
    </section>
  );
}
