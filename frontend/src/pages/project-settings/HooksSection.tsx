import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryStatus } from "@/components/QueryStatus";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api";
import { ActionError } from "@/pages/incidents/ActionError";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import { HookCredential } from "./HookCredential";
import { hooksKey, hooksQuery, KIND_LABELS, replaceHook, type Hook } from "./hooksCache";
import { enableLabel } from "./lifecycleActions";
import { LifecycleBadge } from "./LifecycleBadge";
import { policiesKey } from "./policiesKey";

const SHORT_SHA = 12;

function verifiedText(verified: boolean | null | undefined): string {
  if (verified === null || verified === undefined) return "Not verified";
  return verified ? "Yes" : "No";
}

export function HooksSection({ projectId }: { projectId: string }) {
  const hooks = useQuery(hooksQuery(projectId));
  useInvalidateOnEvents(projectId, hooksKey(projectId), [
    "hook.ran",
    "hook.failed",
    "hook.suspended",
    "proposal.decided",
    "handoff.approved",
  ]);
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="hooks-title">
      <h2 id="hooks-title" className="text-lg font-semibold">
        Hooks
      </h2>
      <p className="text-sm text-muted-foreground">
        The commands and workflows agents may run to train, evaluate and deploy.
      </p>
      <QueryStatus query={hooks} loading="Loading hooks..." />
      {hooks.data?.length === 0 && <p className="text-sm text-muted-foreground">No hooks yet.</p>}
      {hooks.data && hooks.data.length > 0 && (
        <ul className="space-y-3">
          {hooks.data.map((hook) => (
            <HookRow key={hook.id} projectId={projectId} hook={hook} />
          ))}
        </ul>
      )}
    </section>
  );
}

function HookRow({ projectId, hook }: { projectId: string; hook: Hook }) {
  const queryClient = useQueryClient();
  const params = { path: { project_id: projectId, hook_id: hook.id } };
  const setEnabled = useMutation({
    mutationFn: async (enabled: boolean) =>
      unwrap(
        await api.PUT("/api/projects/{project_id}/hooks/{hook_id}/enabled", {
          params,
          body: { enabled },
        }),
      ),
    onSuccess: (saved) => {
      replaceHook(queryClient, projectId, saved);
    },
  });
  const approve = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/projects/{project_id}/hooks/{hook_id}/approve", { params })),
    onSuccess: async (saved) => {
      replaceHook(queryClient, projectId, saved);
      // Approving a hook also puts the policies that wait on it back in service.
      await queryClient.invalidateQueries({ queryKey: policiesKey(projectId) });
    },
  });
  const busy = setEnabled.isPending || approve.isPending;
  const refused = setEnabled.error ?? approve.error;

  return (
    <li aria-label={hook.name} className="space-y-3 rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{hook.name}</span>
        <Badge variant="outline">{KIND_LABELS[hook.spec.kind]}</Badge>
        <LifecycleBadge status={hook.status} />
        <div className="ml-auto flex flex-wrap gap-2">
          {hook.status === "suspended" && (
            <Button
              size="sm"
              disabled={busy}
              onClick={() => {
                setEnabled.reset();
                approve.mutate();
              }}
            >
              Approve changed content
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => {
              approve.reset();
              setEnabled.mutate(hook.status === "disabled");
            }}
          >
            {enableLabel(hook.status)}
          </Button>
        </div>
      </div>
      {hook.suspended_reason && <p className="text-destructive">{hook.suspended_reason}</p>}
      <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">Model</dt>
          <dd>{hook.model ?? "–"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Deploys</dt>
          <dd data-testid="hook-deploys">{hook.spec.deploys ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Deploy verified</dt>
          <dd data-testid="hook-deploys-verified">{verifiedText(hook.deploys_verified)}</dd>
        </div>
      </dl>
      {hook.pins.length > 0 && (
        <div>
          <p className="text-muted-foreground">Pinned files</p>
          <ul className="space-y-0.5 font-mono text-xs">
            {hook.pins.map((pin) => (
              <li key={pin.path} className="break-all">
                <span>{pin.path}</span> <span>{pin.sha256.slice(0, SHORT_SHA)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <ActionError error={refused} />
      <HookCredential projectId={projectId} hook={hook} />
    </li>
  );
}
