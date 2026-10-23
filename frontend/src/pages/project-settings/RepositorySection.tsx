import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { components } from "@/api/schema";
import { FolderBrowser } from "@/components/FolderBrowser";
import { ProjectStatus } from "@/components/ProjectStatus";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, errorText, unwrap } from "@/lib/api";
import { useAppInfo } from "@/lib/useAppInfo";

export function RepositorySection({ project }: { project: components["schemas"]["Project"] }) {
  const queryClient = useQueryClient();
  const [relinkOpen, setRelinkOpen] = useState(false);
  const params = { path: { project_id: project.id } };
  const app = useAppInfo();
  const updateProject = (updated: components["schemas"]["Project"]) => {
    queryClient.setQueryData(["projects", project.id], updated);
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
  };
  const refresh = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/projects/{project_id}/refresh", { params })),
    onSuccess: updateProject,
  });
  const relink = useMutation({
    mutationFn: async (path: string) =>
      unwrap(await api.POST("/api/projects/{project_id}/relink", { params, body: { path } })),
    onSuccess: (updated) => {
      updateProject(updated);
      setRelinkOpen(false);
    },
  });
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="repository-title">
      <h2 id="repository-title" className="text-lg font-semibold">
        Repository
      </h2>
      <dl className="grid gap-2 text-sm sm:grid-cols-[10rem_1fr]">
        <dt className="text-muted-foreground">Name</dt>
        <dd>{project.name}</dd>
        <dt className="text-muted-foreground">Source</dt>
        <dd>{project.source === "local" ? "Local folder" : "GitHub"}</dd>
        <dt className="text-muted-foreground">
          {project.source === "local" ? "Local path" : "Repository"}
        </dt>
        <dd className="break-all">
          {project.source === "local" ? project.local_path : project.github_full_name}
        </dd>
        <dt className="text-muted-foreground">Default branch</dt>
        <dd>{project.default_branch ?? "None"}</dd>
      </dl>
      <ProjectStatus status={project.status} status_detail={project.status_detail} />
      <div className="flex gap-2">
        <Button
          variant="outline"
          disabled={refresh.isPending}
          onClick={() => {
            refresh.mutate();
          }}
        >
          {refresh.isPending ? "Refreshing..." : "Refresh"}
        </Button>
        {project.source === "local" && app.data?.mode === "local" && (
          <Button
            variant="outline"
            onClick={() => {
              relink.reset();
              setRelinkOpen(true);
            }}
          >
            Relink
          </Button>
        )}
      </div>
      {refresh.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(refresh.error)}
        </p>
      )}
      {app.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(app.error)}
        </p>
      )}
      <Dialog
        open={relinkOpen}
        onOpenChange={(open) => {
          if (!relink.isPending) setRelinkOpen(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Relink project</DialogTitle>
            <DialogDescription>Choose the new location of this repository.</DialogDescription>
          </DialogHeader>
          <fieldset disabled={relink.isPending} className="min-w-0">
            <FolderBrowser
              initialPath={project.local_path ?? undefined}
              onChoose={(path) => {
                relink.mutate(path);
              }}
            />
          </fieldset>
          {relink.isPending && <p role="status">Relinking...</p>}
          {relink.error && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(relink.error)}
            </p>
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}
