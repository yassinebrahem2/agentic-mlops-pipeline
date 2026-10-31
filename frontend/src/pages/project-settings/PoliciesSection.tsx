import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api";
import { ActionError } from "@/pages/incidents/ActionError";
import { formatTime } from "@/lib/format";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import { enableLabel } from "./lifecycleActions";
import { LifecycleBadge } from "./LifecycleBadge";
import { policiesKey } from "./policiesKey";

type Policy = components["schemas"]["TrainingPolicyView"];
type Window = Policy["payload"]["window"];

function windowText(window: Window): string {
  switch (window.kind) {
    case "trailing_days":
      return window.days ? `Trailing ${String(window.days)} days` : "Trailing days";
    case "since_last_run":
      return "Since the last run";
    case "fixed":
      return `${formatTime(window.start)} to ${formatTime(window.end)}`;
  }
}

export function PoliciesSection({ projectId }: { projectId: string }) {
  const params = { path: { project_id: projectId } };
  const policies = useQuery({
    queryKey: policiesKey(projectId),
    queryFn: async () =>
      unwrap(await api.GET("/api/projects/{project_id}/training/policies", { params })),
  });
  useInvalidateOnEvents(projectId, policiesKey(projectId), ["policy.suspended"]);
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="policies-title">
      <h2 id="policies-title" className="text-lg font-semibold">
        Training policies
      </h2>
      <p className="text-sm text-muted-foreground">
        What an agent may train on its own: the hooks it runs and how often.
      </p>
      <QueryStatus query={policies} loading="Loading training policies..." />
      {policies.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">No training policies yet.</p>
      )}
      {policies.data && policies.data.length > 0 && (
        <ul className="space-y-3">
          {policies.data.map((policy) => (
            <PolicyRow key={policy.id} projectId={projectId} policy={policy} />
          ))}
        </ul>
      )}
    </section>
  );
}

function PolicyRow({ projectId, policy }: { projectId: string; policy: Policy }) {
  const queryClient = useQueryClient();
  const { payload } = policy;
  const setEnabled = useMutation({
    mutationFn: async (enabled: boolean) =>
      unwrap(
        await api.PUT("/api/projects/{project_id}/training/policies/{policy_id}/enabled", {
          params: { path: { project_id: projectId, policy_id: policy.id } },
          body: { enabled },
        }),
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData<Policy[]>(policiesKey(projectId), (old) =>
        old?.map((entry) => (entry.id === saved.id ? saved : entry)),
      );
    },
  });
  return (
    <li
      aria-label={`Policy for ${payload.model}`}
      className="space-y-2 rounded-md border p-3 text-sm"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{payload.model}</span>
        <LifecycleBadge status={policy.status} />
        <Button
          className="ml-auto"
          size="sm"
          variant="outline"
          disabled={setEnabled.isPending}
          onClick={() => {
            setEnabled.mutate(policy.status === "disabled");
          }}
        >
          {enableLabel(policy.status)}
        </Button>
      </div>
      {policy.suspended_reason && <p className="text-destructive">{policy.suspended_reason}</p>}
      <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">Training hook</dt>
          <dd className="break-all font-mono text-xs">{payload.train_hook_id}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Evaluation hook</dt>
          <dd className="break-all font-mono text-xs">{payload.evaluate_hook_id ?? "–"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Training window</dt>
          <dd>{windowText(payload.window)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Limits</dt>
          <dd>
            At least {payload.min_interval_hours} hours apart, {payload.max_runs_per_week} runs per
            week, {payload.run_time_limit_minutes} minutes per run
          </dd>
        </div>
      </dl>
      <ActionError error={setEnabled.error} />
    </li>
  );
}
