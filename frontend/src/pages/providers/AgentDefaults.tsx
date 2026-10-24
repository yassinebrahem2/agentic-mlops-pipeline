import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId } from "react";
import type { components } from "@/api/schema";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, errorText, unwrap } from "@/lib/api";
import { hostedModelWarning } from "@/lib/models";
import { fromSelectValue, SELECT_NONE, toSelectValue } from "@/lib/selectNone";
import { useModels } from "@/lib/useModels";
import { useProviders } from "@/lib/useProviders";

type AgentInfo = components["schemas"]["AgentInfo"];
type ModelEntry = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];

function AgentRow({
  agent,
  models,
  providers,
}: {
  agent: AgentInfo;
  models: ModelEntry[];
  providers: Provider[];
}) {
  const queryClient = useQueryClient();
  const selectId = useId();

  const updateModel = useMutation({
    mutationFn: async (address: string | null) =>
      unwrap(
        await api.PUT("/api/agents/{agent}/model", {
          params: { path: { agent: agent.name } },
          body: { address },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  const usableModels = models.filter((m) => m.problem === null);
  const currentModel = agent.settings.default_model;
  const isCurrentUsable =
    currentModel !== null && usableModels.some((m) => m.address === currentModel);
  const showNotUsable = currentModel !== null && !isCurrentUsable;

  const modelObj = currentModel ? models.find((m) => m.address === currentModel) : undefined;
  const providerObj = modelObj
    ? providers.find((p) => p.name === modelObj.provider_name)
    : undefined;

  return (
    <div className="flex flex-col gap-3 rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="text-base font-semibold">{agent.display_name}</h3>
          <p className="text-sm text-muted-foreground">{agent.description}</p>
        </div>
        <div className="flex flex-col gap-1 sm:items-end">
          <div className="flex items-center gap-2">
            <Label htmlFor={selectId} className="text-sm font-medium">
              Model for {agent.display_name}
            </Label>
            <Select
              value={toSelectValue(currentModel)}
              disabled={updateModel.isPending}
              onValueChange={(val) => {
                updateModel.mutate(fromSelectValue(val));
              }}
            >
              <SelectTrigger id={selectId}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_NONE}>None</SelectItem>
                {usableModels.map((m) => (
                  <SelectItem key={m.address} value={m.address}>
                    {m.address}
                  </SelectItem>
                ))}
                {showNotUsable && (
                  <SelectItem key={currentModel} value={currentModel}>
                    {currentModel} (not usable)
                  </SelectItem>
                )}
              </SelectContent>
            </Select>
          </div>
        </div>
      </div>

      {agent.settings.model_problem && (
        <p className="text-sm text-amber-600 dark:text-amber-400">{agent.settings.model_problem}</p>
      )}

      {providerObj?.kind === "hosted" && (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          {hostedModelWarning(providerObj.name)}
        </p>
      )}

      {updateModel.isError && (
        <p className="text-sm text-destructive">{errorText(updateModel.error)}</p>
      )}
    </div>
  );
}

export function AgentDefaults() {
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: async () => unwrap(await api.GET("/api/agents")),
  });
  const models = useModels();
  const providers = useProviders();

  const isPending = agents.isPending || models.isPending || providers.isPending;
  const hasError = agents.isError || models.isError || providers.isError;
  const isReady = agents.isSuccess && models.isSuccess && providers.isSuccess;

  return (
    <section aria-labelledby="agent-defaults-heading" className="flex flex-col gap-4">
      <h2 id="agent-defaults-heading" className="text-lg font-medium">
        Default model per agent
      </h2>
      {isPending && !hasError && <p className="text-muted-foreground">Loading default models…</p>}
      {agents.isError && <p className="text-destructive">{errorText(agents.error)}</p>}
      {models.isError && <p className="text-destructive">{errorText(models.error)}</p>}
      {providers.isError && <p className="text-destructive">{errorText(providers.error)}</p>}
      {isReady && agents.data.length === 0 && (
        <p className="text-muted-foreground">No agents found.</p>
      )}
      {isReady &&
        agents.data.map((agent) => (
          <AgentRow
            key={agent.name}
            agent={agent}
            models={models.data}
            providers={providers.data}
          />
        ))}
    </section>
  );
}
