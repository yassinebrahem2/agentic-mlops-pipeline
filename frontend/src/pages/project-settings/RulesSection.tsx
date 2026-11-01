import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import { ActionError } from "@/pages/incidents/ActionError";
import { RuleTermItem } from "@/pages/agents/proposals/RuleProposalCard";
import { hooksQuery, KIND_LABELS } from "./hooksCache";
import { enableLabel } from "./lifecycleActions";
import { LifecycleBadge } from "./LifecycleBadge";
import { rulesKey } from "./rulesKey";

type Rule = components["schemas"]["RuleView"];

export function RulesSection({ projectId }: { projectId: string }) {
  const rules = useQuery({
    queryKey: rulesKey(projectId),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/rules", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
  // Only to show a hook's name; a hooks list that fails to load leaves the id visible.
  const hooks = useQuery(hooksQuery(projectId));
  useInvalidateOnEvents(projectId, rulesKey(projectId), [
    "rule.satisfied",
    "proposal.decided",
    "handoff.approved",
  ]);
  const hookNames = new Map(hooks.data?.map((hook) => [hook.id, hook.name]));
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="rules-title">
      <h2 id="rules-title" className="text-lg font-semibold">
        Rules
      </h2>
      <p className="text-sm text-muted-foreground">
        When monitoring checks match a rule&apos;s terms, its hook runs within the rule&apos;s
        limits.
      </p>
      <QueryStatus query={rules} loading="Loading rules..." />
      {rules.data?.length === 0 && (
        <div className="text-sm text-muted-foreground">
          <p>No rules yet.</p>
          <p>Rules are created through approved proposals.</p>
        </div>
      )}
      {rules.data && rules.data.length > 0 && (
        <ul className="space-y-3">
          {rules.data.map((rule) => (
            <RuleRow
              key={rule.id}
              projectId={projectId}
              rule={rule}
              hookName={hookNames.get(rule.spec.hook_id)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function RuleRow({
  projectId,
  rule,
  hookName,
}: {
  projectId: string;
  rule: Rule;
  hookName: string | undefined;
}) {
  const queryClient = useQueryClient();
  const { spec } = rule;
  const status = rule.enabled ? "active" : "disabled";
  const setEnabled = useMutation({
    mutationFn: async (enabled: boolean) =>
      unwrap(
        await api.PUT("/api/projects/{project_id}/rules/{rule_id}/enabled", {
          params: { path: { project_id: projectId, rule_id: rule.id } },
          body: { enabled },
        }),
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData<Rule[]>(rulesKey(projectId), (old) =>
        old?.map((entry) => (entry.id === saved.id ? saved : entry)),
      );
    },
  });
  const action = enableLabel(status);
  return (
    <li aria-label={spec.name} className="space-y-3 rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{spec.name}</span>
        <LifecycleBadge status={status} />
        <Button
          className="ml-auto"
          size="sm"
          variant="outline"
          aria-label={`${action} ${spec.name}`}
          disabled={setEnabled.isPending}
          onClick={() => {
            setEnabled.mutate(!rule.enabled);
          }}
        >
          {action}
        </Button>
      </div>
      <p className="text-muted-foreground">{spec.why}</p>
      <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">Model</dt>
          <dd>{spec.model ?? "Project-wide"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Hook</dt>
          <dd className="flex flex-wrap items-center gap-2">
            <span className={hookName ? undefined : "break-all font-mono text-xs"}>
              {hookName ?? spec.hook_id}
            </span>
            <Badge variant="outline">{KIND_LABELS[spec.hook_kind]}</Badge>
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Limits</dt>
          <dd>
            {spec.limits.per_incident} per incident, {spec.limits.per_day} per day
          </dd>
        </div>
      </dl>
      <div className="space-y-1.5">
        <p className="text-muted-foreground">Terms</p>
        {spec.terms.map((term, index) => (
          <RuleTermItem key={`${term.check_id}-${String(index)}`} term={term} index={index} />
        ))}
      </div>
      <ActionError error={setEnabled.error} />
    </li>
  );
}
