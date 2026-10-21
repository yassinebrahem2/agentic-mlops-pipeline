import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { api, errorText, unwrap } from "@/lib/api";

type DetectedKey = components["schemas"]["DetectedKey"];

export function DetectedKeys() {
  const detected = useQuery({
    queryKey: ["env-keys"],
    queryFn: async () => unwrap(await api.GET("/api/env-keys")),
  });

  if (!detected.data || detected.data.length === 0) return null;
  return (
    <section aria-labelledby="env-keys-heading" className="flex flex-col gap-4">
      <div>
        <h2 id="env-keys-heading" className="text-lg font-medium">
          Keys found in the environment
        </h2>
        <p className="text-muted-foreground">
          The server can see these environment variables. Importing one saves the key for its
          provider, adding the provider if it is not there yet.
        </p>
      </div>
      <ul className="flex flex-col gap-2">
        {detected.data.map((key) => (
          <DetectedKeyItem key={key.env_var} detected={key} />
        ))}
      </ul>
    </section>
  );
}

function DetectedKeyItem({ detected }: { detected: DetectedKey }) {
  const queryClient = useQueryClient();
  const importKey = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/env-keys/{env_var}/import", {
          params: { path: { env_var: detected.env_var } },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["providers"] }),
        queryClient.invalidateQueries({ queryKey: ["env-keys"] }),
      ]);
    },
  });
  const action = detected.provider_exists ? "Import key" : "Import";

  return (
    <li className="flex flex-col gap-1 rounded-md border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <code className="text-sm font-medium">{detected.env_var}</code>
          <span>{detected.provider_name}</span>
          <code className="text-sm text-muted-foreground">{detected.hint}</code>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={importKey.isPending}
          aria-label={`${action} ${detected.env_var}`}
          onClick={() => {
            importKey.mutate();
          }}
        >
          {action}
        </Button>
      </div>
      {importKey.isError && <p className="text-destructive">{errorText(importKey.error)}</p>}
    </li>
  );
}
