import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";
import { hostedModelWarning } from "@/lib/models";

type Agent = components["schemas"]["AgentInfo"];
type Model = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];

export function AgentsSection({ projectId }: { projectId: string }) {
  const agents = useQuery({
    queryKey: ["projects", projectId, "agents"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/agents", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
  const models = useQuery({
    queryKey: ["models"],
    queryFn: async () => unwrap(await api.GET("/api/models")),
  });
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: async () => unwrap(await api.GET("/api/providers")),
  });
  return (
    <section className="space-y-4" aria-labelledby="agents-title">
      <h2 id="agents-title" className="text-lg font-semibold">
        Agents
      </h2>
      {[agents.error, models.error, providers.error].filter(Boolean).map((error, index) => (
        <p key={index} role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      ))}
      {(agents.isPending || models.isPending || providers.isPending) && (
        <p role="status">Loading agents...</p>
      )}
      {agents.data &&
        models.data &&
        providers.data &&
        agents.data.map((agent) => (
          <AgentRow
            key={`${projectId}:${agent.name}`}
            projectId={projectId}
            agent={agent}
            models={models.data}
            providers={providers.data}
          />
        ))}
    </section>
  );
}

function AgentRow({
  projectId,
  agent,
  models,
  providers,
}: {
  projectId: string;
  agent: Agent;
  models: Model[];
  providers: Provider[];
}) {
  const queryClient = useQueryClient();
  const params = { path: { project_id: projectId, agent: agent.name } };
  const settings = agent.settings;
  const usable = models.filter((model) => model.problem === null);
  const unusableOverride =
    settings.project_model !== null &&
    !usable.some((model) => model.address === settings.project_model);
  const effectiveModel = models.find((model) => model.address === settings.effective_model);
  const provider = providers.find((provider) => provider.name === effectiveModel?.provider_name);
  const updateAgent = (updated: Agent) => {
    queryClient.setQueryData<Agent[]>(["projects", projectId, "agents"], (current) =>
      current?.map((entry) => (entry.name === updated.name ? updated : entry)),
    );
    void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "agents"] });
  };
  const model = useMutation({
    mutationFn: async (address: string | null) =>
      unwrap(
        await api.PUT("/api/projects/{project_id}/agents/{agent}/model", {
          params,
          body: { address },
        }),
      ),
    onSuccess: updateAgent,
  });
  const limits = useMutation({
    mutationFn: async (max_steps: number | null) =>
      unwrap(
        await api.PUT("/api/projects/{project_id}/agents/{agent}/limits", {
          params,
          body: { max_steps },
        }),
      ),
    onSuccess: updateAgent,
  });
  return (
    <article className="space-y-4 rounded-lg border p-4" aria-labelledby={`${agent.name}-title`}>
      <h3 id={`${agent.name}-title`} className="font-semibold">
        {agent.display_name}
      </h3>
      <div className="space-y-2">
        <Label htmlFor={`${agent.name}-model`}>Model</Label>
        <select
          id={`${agent.name}-model`}
          className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          value={settings.project_model ?? ""}
          disabled={model.isPending}
          onChange={(event) => {
            model.mutate(event.target.value || null);
          }}
        >
          <option value="">Use the app default ({settings.default_model ?? "none"})</option>
          {usable.map((entry) => (
            <option key={entry.address} value={entry.address}>
              {entry.address}
            </option>
          ))}
          {unusableOverride && (
            <option value={settings.project_model ?? ""}>
              {settings.project_model} (not usable)
            </option>
          )}
        </select>
        {model.error && (
          <p role="alert" className="text-sm text-destructive">
            {errorText(model.error)}
          </p>
        )}
        {settings.model_problem && (
          <p className="text-sm text-amber-600 dark:text-amber-400">{settings.model_problem}</p>
        )}
        {provider?.kind === "hosted" && (
          <p className="text-sm text-amber-600 dark:text-amber-400">
            {hostedModelWarning(provider.name)}
          </p>
        )}
      </div>
      <LimitsForm
        key={String(settings.max_steps)}
        agent={agent.name}
        maxSteps={settings.max_steps}
        pending={limits.isPending}
        onSave={(steps) => {
          limits.mutate(steps);
        }}
      />
      {limits.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(limits.error)}
        </p>
      )}
    </article>
  );
}

function LimitsForm({
  agent,
  maxSteps,
  pending,
  onSave,
}: {
  agent: string;
  maxSteps: number | null;
  pending: boolean;
  onSave: (steps: number | null) => void;
}) {
  const [steps, setSteps] = useState(maxSteps === null ? "" : String(maxSteps));
  const [noLimit, setNoLimit] = useState(maxSteps === null);
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        onSave(noLimit ? null : Number(steps));
      }}
    >
      <div className="space-y-2">
        <Label htmlFor={`${agent}-steps`}>Step limit</Label>
        <Input
          id={`${agent}-steps`}
          type="number"
          min={1}
          step={1}
          required={!noLimit}
          disabled={noLimit || pending}
          value={steps}
          onChange={(event) => {
            setSteps(event.target.value);
          }}
        />
      </div>
      <label className="flex h-9 items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={noLimit}
          disabled={pending}
          onChange={(event) => {
            setNoLimit(event.target.checked);
          }}
        />
        No limit
      </label>
      <Button type="submit" variant="outline" disabled={pending}>
        {pending ? "Saving..." : "Save step limit"}
      </Button>
    </form>
  );
}
