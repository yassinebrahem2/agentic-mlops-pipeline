import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { formatTime } from "@/lib/format";
import { useTrackingSources, type TrackingSourceView } from "@/lib/useTracking";
import { AddTrackingSourceDialog } from "./AddTrackingSourceDialog";
import { SourceCredentialForm } from "./SourceCredentialForm";

function sourceTypeLabel(type: TrackingSourceView["type"]): string {
  if (type === "mlflow") return "MLflow";
  return "GitHub Actions";
}

function sourceTarget(source: TrackingSourceView): string {
  const settings = (source as Partial<TrackingSourceView>).settings ?? {};
  if (source.type === "mlflow") {
    return typeof settings.url === "string" ? settings.url : "";
  }
  const repo = typeof settings.repository === "string" ? settings.repository : "";
  const workflows = Array.isArray(settings.workflows)
    ? settings.workflows.filter((w): w is string => typeof w === "string")
    : [];
  const wfText = workflows.length > 0 ? workflows.join(", ") : "all workflows";
  return repo ? `${repo} (${wfText})` : wfText;
}

function authLabel(source: TrackingSourceView): string {
  if (source.auth === "credential") {
    return source.credential_set ? "Credential set" : "Credential missing";
  }
  return "No authentication";
}

function lastSyncLabel(source: TrackingSourceView): string {
  if (source.last_sync) {
    return `Last sync ${formatTime(source.last_sync)}`;
  }
  return "Never synced";
}

export function TrackingSourcesSection({ projectId }: { projectId: string }) {
  const [addOpen, setAddOpen] = useState(false);
  const [credentialErrors, setCredentialErrors] = useState<Record<string, string>>({});
  const sources = useTrackingSources(projectId);

  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="tracking-sources-title">
      <div className="flex items-center justify-between">
        <div>
          <h2 id="tracking-sources-title" className="text-lg font-semibold">
            Tracking sources
          </h2>
          <p className="text-sm text-muted-foreground">
            Experiment and run tracking sources for this project.
          </p>
        </div>
        <Button
          onClick={() => {
            setAddOpen(true);
          }}
        >
          Add tracking source
        </Button>
      </div>

      {sources.isPending && <p role="status">Loading tracking sources...</p>}

      {sources.error && <p className="text-sm text-destructive">{errorText(sources.error)}</p>}

      {sources.data && sources.data.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No tracking source yet. Onboarding adds them when you approve the handoff, or add one
          here.
        </p>
      )}

      {sources.data && sources.data.length > 0 && (
        <div className="space-y-4">
          {sources.data.map((source) => (
            <div key={source.id} className="space-y-3 rounded-lg border p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-base font-semibold">{sourceTypeLabel(source.type)}</span>
                  <Badge variant={source.credential_set ? "outline" : "secondary"}>
                    {authLabel(source)}
                  </Badge>
                </div>
                <span className="text-sm text-muted-foreground">{lastSyncLabel(source)}</span>
              </div>

              <div className="text-sm">
                <span className="font-mono">{sourceTarget(source)}</span>
              </div>

              {source.last_error && (
                <Alert variant="destructive">
                  <AlertDescription>{source.last_error}</AlertDescription>
                </Alert>
              )}

              {source.auth === "credential" && (
                <SourceCredentialForm
                  projectId={projectId}
                  sourceId={source.id}
                  initialError={credentialErrors[source.id]}
                  onSuccess={() => {
                    setCredentialErrors((prev) =>
                      Object.fromEntries(Object.entries(prev).filter(([key]) => key !== source.id)),
                    );
                  }}
                />
              )}
            </div>
          ))}
        </div>
      )}

      <AddTrackingSourceDialog
        projectId={projectId}
        open={addOpen}
        onOpenChange={setAddOpen}
        onCredentialError={(sourceId, err) => {
          setCredentialErrors((prev) => ({ ...prev, [sourceId]: err }));
        }}
      />
    </section>
  );
}
