import { useParams } from "react-router";
import { AlertCircle, AlertTriangle, Boxes, Loader2, RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { useHandoff } from "@/lib/useHandoff";
import { useSyncNow, useTrackedModels, useTrackingSources } from "@/lib/useTracking";
import { ModelCard } from "./ModelCard";

const SOURCE_TYPE_LABELS: Record<string, string> = {
  mlflow: "MLflow",
  github_actions: "GitHub Actions",
};

export function TrackedModelsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return null;
  return <TrackedModelsView projectId={projectId} />;
}

function TrackedModelsView({ projectId }: { projectId: string }) {
  const {
    data: sources,
    isLoading: sourcesLoading,
    error: sourcesError,
  } = useTrackingSources(projectId);

  const {
    data: modelsPage,
    isLoading: modelsLoading,
    error: modelsError,
  } = useTrackedModels(projectId);

  const { data: handoff } = useHandoff(projectId);

  const syncMutation = useSyncNow(projectId);

  const isLoading = sourcesLoading || modelsLoading;

  return (
    <div className="container max-w-5xl py-6 space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div className="space-y-1">
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            <Boxes className="size-6 text-primary" />
            Models
          </h1>
          <p className="text-sm text-muted-foreground">
            Tracked models and versions across your project.
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

      {sources?.map((source) => {
        if (!source.last_error) return null;
        const typeLabel = SOURCE_TYPE_LABELS[source.type] ?? source.type;
        return (
          <Alert key={source.id} variant="warning">
            <AlertTriangle className="size-4" />
            <AlertTitle>Sync warning ({typeLabel})</AlertTitle>
            <AlertDescription>{source.last_error}</AlertDescription>
          </Alert>
        );
      })}

      {sourcesError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading tracking sources</AlertTitle>
          <AlertDescription>{errorText(sourcesError)}</AlertDescription>
        </Alert>
      )}

      {modelsError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading models</AlertTitle>
          <AlertDescription>{errorText(modelsError)}</AlertDescription>
        </Alert>
      )}

      {isLoading && (
        <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading models...
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
        <div className="space-y-6">
          {!modelsPage || modelsPage.rows.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center">
              <p className="text-sm text-muted-foreground">No models tracked yet</p>
            </div>
          ) : (
            <div className="space-y-6">
              {modelsPage.rows.map((model) => (
                <ModelCard
                  key={`${model.source_id}:${model.name}`}
                  projectId={projectId}
                  model={model}
                  handoff={handoff ?? null}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
