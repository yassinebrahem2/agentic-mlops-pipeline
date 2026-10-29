import { EmbedderSelect } from "@/components/EmbedderSelect";
import { errorText } from "@/lib/api";
import { useEmbedderSettings } from "@/lib/useEmbedderSettings";
import { useModels } from "@/lib/useModels";
import { useProviders } from "@/lib/useProviders";

export function EmbedderSection() {
  const embedder = useEmbedderSettings();
  const models = useModels();
  const providers = useProviders();

  return (
    <section aria-labelledby="embedder-heading" className="flex flex-col gap-4">
      <div>
        <h2 id="embedder-heading" className="text-lg font-medium">
          Embedder
        </h2>
        <p className="text-muted-foreground">
          Notes are searched by meaning with this embedder. With none set, search uses full text
          only.
        </p>
      </div>

      {[embedder.error, models.error, providers.error].filter(Boolean).map((error, index) => (
        <p key={index} role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      ))}

      {(embedder.isPending || models.isPending || providers.isPending) && (
        <p role="status" className="text-muted-foreground">
          Loading…
        </p>
      )}

      {embedder.data?.status && models.data && providers.data && (
        <div className="space-y-2">
          <EmbedderSelect
            id="default-embedder"
            label="Default embedder"
            noneLabel="None: search uses full text only"
            value={embedder.data.chosen}
            onChange={(val) => {
              embedder.mutation.mutate(val);
            }}
            disabled={embedder.mutation.isPending}
            models={models.data}
            providers={providers.data}
          />
          {embedder.data.status.problem && (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              {embedder.data.status.problem}
            </p>
          )}
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
