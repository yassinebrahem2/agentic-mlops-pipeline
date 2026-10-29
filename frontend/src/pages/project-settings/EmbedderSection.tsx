import type { components } from "@/api/schema";
import { EmbedderSelect } from "@/components/EmbedderSelect";
import { errorText } from "@/lib/api";
import { useEmbedderSettings } from "@/lib/useEmbedderSettings";
import { useModels } from "@/lib/useModels";
import { useProviders } from "@/lib/useProviders";

type EmbedderStatus = components["schemas"]["EmbedderStatus"];

function statusLine(status?: EmbedderStatus): string {
  if (!status) {
    return "Using full text only";
  }
  if (status.problem) {
    return `Using full text only: ${status.problem}`;
  }
  if (status.address) {
    return `Using ${status.address}`;
  }
  return "Using full text only";
}

export function EmbedderSection({ projectId }: { projectId: string }) {
  const embedder = useEmbedderSettings(projectId);
  const models = useModels();
  const providers = useProviders();

  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="embedder-title">
      <h2 id="embedder-title" className="text-lg font-semibold">
        Embedder
      </h2>

      {[embedder.error, models.error, providers.error].filter(Boolean).map((error, index) => (
        <p key={index} className="text-sm text-destructive">
          {errorText(error)}
        </p>
      ))}

      {(embedder.isPending || models.isPending || providers.isPending) && (
        <p role="status">Loading embedder settings...</p>
      )}

      {embedder.data?.status && models.data && providers.data && (
        <div className="space-y-2">
          <EmbedderSelect
            id="project-embedder"
            label="Embedder for this project"
            noneLabel="Use the app default"
            value={embedder.data.chosen}
            onChange={(val) => {
              embedder.mutation.mutate(val);
            }}
            disabled={embedder.mutation.isPending}
            models={models.data}
            providers={providers.data}
          />
          <p
            className={
              embedder.data.status.problem
                ? "text-sm text-amber-600 dark:text-amber-400"
                : "text-sm text-muted-foreground"
            }
          >
            {statusLine(embedder.data.status)}
          </p>
          {embedder.mutation.error && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(embedder.mutation.error)}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
