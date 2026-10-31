import { Link } from "react-router";
import { Badge } from "@/components/ui/badge";
import { formatTime } from "@/lib/format";
import { entryIdentity, type ActivityEntry } from "./useActivity";

// Payloads and audit details are already scrubbed by the server; React renders them as text.
function Details({ value }: { value: Record<string, unknown> }) {
  if (Object.keys(value).length === 0) return null;
  return (
    <details className="mt-2 text-xs">
      <summary className="cursor-pointer text-muted-foreground">Details</summary>
      <pre className="mt-1 max-h-64 overflow-auto rounded bg-muted p-2 whitespace-pre-wrap break-words">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}

export function ActivityEntryRow({
  entry,
  projectId,
}: {
  entry: ActivityEntry;
  projectId: string;
}) {
  const testId = `activity-${entryIdentity(entry)}`;
  const isEvent = entry.kind === "event";
  const who = isEvent ? entry.event.source : entry.audit.actor;

  return (
    <li className="rounded-lg border border-border/80 bg-card p-3 shadow-xs" data-testid={testId}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <time dateTime={entry.ts} className="text-xs text-muted-foreground">
          {formatTime(entry.ts)}
        </time>
        <Badge variant={isEvent ? "secondary" : "outline"}>{isEvent ? "Event" : "Audit"}</Badge>
        <span className="font-medium">{who}</span>
        {entry.kind === "event" ? (
          <>
            <span className="font-mono text-xs">{entry.event.type}</span>
            {entry.incident_id !== null && (
              <Link
                to={`/projects/${projectId}/incidents/${entry.incident_id}`}
                className="text-xs text-primary underline underline-offset-2"
              >
                {entry.incident_id}
              </Link>
            )}
          </>
        ) : (
          <>
            <span className="font-mono text-xs">{entry.audit.action}</span>
            <span className="min-w-0 truncate text-xs text-muted-foreground">
              {entry.audit.target}
            </span>
            <Badge variant="outline">{entry.audit.decision}</Badge>
          </>
        )}
      </div>
      <Details value={entry.kind === "event" ? entry.event.payload : entry.audit.detail} />
    </li>
  );
}
