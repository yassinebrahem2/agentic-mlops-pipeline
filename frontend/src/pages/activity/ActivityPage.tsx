import { useMemo, useState, type SubmitEvent } from "react";
import { useParams } from "react-router";
import { AlertCircle, Loader2, ScrollText } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { errorText } from "@/lib/api";
import { fromSelectValue, SELECT_NONE, toSelectValue } from "@/lib/selectNone";
import { PROJECT_EVENT_TYPES, type ProjectEventType } from "@/lib/useProjectEvents";
import { ActivityEntryRow } from "./ActivityEntryRow";
import { flattenPages, useActivity } from "./useActivity";

export function ActivityPage() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return null;
  return <Activity projectId={projectId} />;
}

function Activity({ projectId }: { projectId: string }) {
  const [type, setType] = useState<ProjectEventType | undefined>(undefined);
  // The agent box is applied on submit, so typing does not query on every keystroke.
  const [agentText, setAgentText] = useState("");
  const [source, setSource] = useState<string | undefined>(undefined);

  const query = useActivity(projectId, { type, source });
  const entries = useMemo(() => flattenPages(query.data?.pages ?? []), [query.data]);

  const applyAgent = (event: SubmitEvent) => {
    event.preventDefault();
    const trimmed = agentText.trim();
    setSource(trimmed === "" ? undefined : trimmed);
  };

  const retry = () => {
    if (query.isFetchNextPageError) void query.fetchNextPage();
    else void query.refetch();
  };

  return (
    <div className="container max-w-4xl py-6 space-y-6">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <ScrollText className="size-6 text-primary" />
          Activity
        </h1>
        <p className="text-sm text-muted-foreground">
          Every action, decision and message in this project, newest first.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <Label htmlFor="activity-type">Event type</Label>
          <Select
            value={toSelectValue(type)}
            onValueChange={(value) => {
              const picked = fromSelectValue(value);
              setType(PROJECT_EVENT_TYPES.find((t) => t === picked));
            }}
          >
            <SelectTrigger id="activity-type" className="w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_NONE}>All event types</SelectItem>
              {PROJECT_EVENT_TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {t}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <form onSubmit={applyAgent} className="flex items-end gap-2">
          <div className="space-y-1.5">
            <Label htmlFor="activity-agent">Agent</Label>
            <Input
              id="activity-agent"
              className="w-48"
              placeholder="All agents"
              value={agentText}
              onChange={(e) => {
                setAgentText(e.target.value);
              }}
            />
          </div>
          <Button type="submit" variant="secondary">
            Apply
          </Button>
        </form>
      </div>
      {type !== undefined && (
        <p className="text-xs text-muted-foreground">
          Audit records have no event type, so they are hidden while an event type is chosen.
        </p>
      )}

      {query.isLoading && (
        <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading activity...
        </div>
      )}

      {query.error && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading activity</AlertTitle>
          <AlertDescription className="space-y-2">
            <p>{errorText(query.error)}</p>
            <Button variant="outline" size="sm" onClick={retry}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {!query.isLoading && !query.error && entries.length === 0 && (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-sm text-muted-foreground">No activity yet.</p>
        </div>
      )}

      {entries.length > 0 && (
        <ul className="space-y-3">
          {entries.map((entry) => (
            <ActivityEntryRow
              key={`${entry.kind}:${entry.kind === "event" ? entry.event.id : entry.audit.id}`}
              entry={entry}
              projectId={projectId}
            />
          ))}
        </ul>
      )}

      {query.hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            disabled={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
          >
            {query.isFetchingNextPage && <Loader2 className="size-4 animate-spin" />}
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
