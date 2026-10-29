import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { formatTime } from "@/lib/format";
import type { TrackingSourceView } from "@/lib/useTracking";

const SOURCE_TYPE_LABELS: Record<string, string> = {
  mlflow: "MLflow",
  github_actions: "GitHub Actions",
};

export function SourceStatus({ sources }: { sources: TrackingSourceView[] }) {
  if (sources.length === 0) return null;

  return (
    <div className="space-y-3">
      {sources.map((source) => {
        const typeLabel = SOURCE_TYPE_LABELS[source.type] ?? source.type;
        const syncText = source.last_sync
          ? `Last sync ${formatTime(source.last_sync)}`
          : "Never synced";

        return (
          <div key={source.id} className="space-y-2 text-sm">
            <div className="flex items-center gap-2 text-muted-foreground">
              <span className="font-medium text-foreground">{typeLabel}</span>
              <span>·</span>
              <span>{syncText}</span>
            </div>
            {source.last_error && (
              <Alert variant="destructive">
                <AlertCircle className="size-4" />
                <AlertTitle>Sync error ({typeLabel})</AlertTitle>
                <AlertDescription>{source.last_error}</AlertDescription>
              </Alert>
            )}
          </div>
        );
      })}
    </div>
  );
}
