import { useMemo, useState } from "react";
import { useParams } from "react-router";
import { Activity, AlertCircle, Loader2, RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorText } from "@/lib/api";
import { useCiRuns, useSyncNow, useTrackedRuns, useTrackingSources } from "@/lib/useTracking";
import { mergeRuns } from "./mergeRuns";
import { RunsTable } from "./RunsTable";
import { SourceStatus } from "./SourceStatus";

export function RunsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return null;
  return <RunsView projectId={projectId} />;
}

function RunsView({ projectId }: { projectId: string }) {
  const [filter, setFilter] = useState<"all" | "failed">("all");

  const {
    data: sources,
    isLoading: sourcesLoading,
    error: sourcesError,
  } = useTrackingSources(projectId);

  const { data: trackedRuns, isLoading: runsLoading, error: runsError } = useTrackedRuns(projectId);

  const { data: ciRuns, isLoading: ciLoading, error: ciError } = useCiRuns(projectId);

  const syncMutation = useSyncNow(projectId);

  const mergedRuns = useMemo(() => {
    return mergeRuns(
      trackedRuns ?? { rows: [], total: 0, truncated: false },
      ciRuns ?? { rows: [], total: 0, truncated: false },
    );
  }, [trackedRuns, ciRuns]);

  const visibleRuns = useMemo(() => {
    if (filter === "failed") {
      return mergedRuns.filter((r) => r.status === "failed");
    }
    return mergedRuns;
  }, [mergedRuns, filter]);

  const isLoading = sourcesLoading || runsLoading || ciLoading;

  return (
    <div className="container max-w-5xl py-6 space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div className="space-y-1">
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            <Activity className="size-6 text-primary" />
            Runs
          </h1>
          <p className="text-sm text-muted-foreground">
            Tracked runs and CI workflows across your project.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            syncMutation.mutate();
          }}
          disabled={syncMutation.isPending}
        >
          {syncMutation.isPending ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            <RefreshCw className="size-4" />
          )}
          Sync now
        </Button>
      </div>

      {syncMutation.error && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Sync failed</AlertTitle>
          <AlertDescription>{errorText(syncMutation.error)}</AlertDescription>
        </Alert>
      )}

      {sourcesError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading tracking sources</AlertTitle>
          <AlertDescription>{errorText(sourcesError)}</AlertDescription>
        </Alert>
      )}

      {runsError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading tracked runs</AlertTitle>
          <AlertDescription>{errorText(runsError)}</AlertDescription>
        </Alert>
      )}

      {ciError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading CI runs</AlertTitle>
          <AlertDescription>{errorText(ciError)}</AlertDescription>
        </Alert>
      )}

      {isLoading && (
        <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading runs...
        </div>
      )}

      {!isLoading && !sourcesError && (!sources || sources.length === 0) && (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-sm text-muted-foreground">
            No tracking source yet. Onboarding creates them when you approve the handoff.
          </p>
        </div>
      )}

      {!isLoading && sources && sources.length > 0 && (
        <div className="space-y-4">
          <SourceStatus sources={sources} />

          {trackedRuns?.truncated && (
            <p className="text-sm text-muted-foreground">
              Showing the latest {String(trackedRuns.rows.length)} of {String(trackedRuns.total)}
            </p>
          )}
          {ciRuns?.truncated && (
            <p className="text-sm text-muted-foreground">
              Showing the latest {String(ciRuns.rows.length)} of {String(ciRuns.total)}
            </p>
          )}

          {mergedRuns.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center">
              <p className="text-sm text-muted-foreground">Nothing tracked yet. Use Sync now.</p>
            </div>
          ) : (
            <div className="space-y-4">
              <Tabs
                value={filter}
                onValueChange={(val) => {
                  setFilter(val as "all" | "failed");
                }}
              >
                <TabsList>
                  <TabsTrigger value="all">All</TabsTrigger>
                  <TabsTrigger value="failed">Failed</TabsTrigger>
                </TabsList>
              </Tabs>

              {visibleRuns.length === 0 ? (
                <div className="rounded-lg border border-dashed p-8 text-center">
                  <p className="text-sm text-muted-foreground">No failed runs.</p>
                </div>
              ) : (
                <RunsTable items={visibleRuns} />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
