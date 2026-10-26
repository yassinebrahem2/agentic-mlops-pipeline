import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";

type View = components["schemas"]["SandboxView"];
type EnvironmentState = components["schemas"]["EnvironmentStatus"]["state"];

const STATE_LABELS: Record<EnvironmentState, string> = {
  none: "Not built",
  building: "Building",
  ready: "Ready",
  failed: "Failed",
  unavailable: "Unavailable",
};
// While an image builds nothing else tells the page it finished.
const BUILD_POLL_MS = 2000;

const sandboxKey = (projectId: string) => ["projects", projectId, "sandbox"] as const;

export function SandboxSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const params = { path: { project_id: projectId } };
  const sandbox = useQuery({
    queryKey: sandboxKey(projectId),
    queryFn: async () => unwrap(await api.GET("/api/projects/{project_id}/sandbox", { params })),
    refetchInterval: (query) =>
      query.state.data?.environment.state === "building" ? BUILD_POLL_MS : false,
  });
  const update = (change: (view: View) => View) => {
    queryClient.setQueryData<View>(sandboxKey(projectId), (view) => view && change(view));
  };
  const rebuild = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/projects/{project_id}/sandbox/rebuild", { params })),
    onSuccess: (environment) => {
      update((view) => ({ ...view, environment }));
    },
  });
  const network = useMutation({
    mutationFn: async (enabled: boolean) =>
      unwrap(
        await api.PUT("/api/projects/{project_id}/settings/sandbox", {
          params,
          body: { network: enabled },
        }),
      ),
    onSuccess: (saved) => {
      update((view) => ({ ...view, network: saved.network }));
    },
  });
  const view = sandbox.data;
  const cannotRebuild =
    view !== undefined &&
    (!view.docker.available ||
      view.environment.state === "unavailable" ||
      view.environment.state === "building");
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="sandbox-title">
      <h2 id="sandbox-title" className="text-lg font-semibold">
        Sandbox
      </h2>
      {sandbox.isPending && <p role="status">Loading sandbox...</p>}
      {sandbox.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(sandbox.error)}
        </p>
      )}
      {view && (
        <>
          <dl className="grid gap-2 text-sm sm:grid-cols-[10rem_1fr]">
            <dt className="text-muted-foreground">Docker</dt>
            <dd>{view.docker.available ? "Available" : "Not available"}</dd>
            {view.docker.version !== null && (
              <>
                <dt className="text-muted-foreground">Docker version</dt>
                <dd>{view.docker.version}</dd>
              </>
            )}
            {view.docker.detail !== null && (
              <dd className="sm:col-start-2">{view.docker.detail}</dd>
            )}
            <dt className="text-muted-foreground">Environment image</dt>
            <dd>{STATE_LABELS[view.environment.state]}</dd>
            {view.environment.tag !== null && (
              <>
                <dt className="text-muted-foreground">Image tag</dt>
                <dd className="break-all">{view.environment.tag}</dd>
              </>
            )}
            {view.environment.detail !== null && (
              <dd className="sm:col-start-2">{view.environment.detail}</dd>
            )}
          </dl>
          <Button
            variant="outline"
            disabled={cannotRebuild || rebuild.isPending}
            onClick={() => {
              rebuild.mutate();
            }}
          >
            {rebuild.isPending ? "Starting..." : "Rebuild"}
          </Button>
          <div className="flex items-center gap-2">
            <input
              id="sandbox-network"
              type="checkbox"
              checked={view.network}
              disabled={network.isPending}
              onChange={(event) => {
                network.mutate(event.target.checked);
              }}
            />
            <Label htmlFor="sandbox-network">Network access</Label>
          </div>
          <p className="text-sm text-muted-foreground">
            Lets commands that agents run in the sandbox reach the network.
          </p>
        </>
      )}
      {[rebuild.error, network.error].filter(Boolean).map((error, index) => (
        <p key={index} role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      ))}
    </section>
  );
}
