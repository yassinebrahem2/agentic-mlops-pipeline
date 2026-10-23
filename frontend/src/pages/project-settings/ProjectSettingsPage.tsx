import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router";
import { api, errorText, unwrap } from "@/lib/api";
import { RepositorySection } from "./RepositorySection";
import { AgentsSection } from "./AgentsSection";
import { RemoveProjectDialog } from "./RemoveProjectDialog";

export function ProjectSettingsPage() {
  const { projectId = "" } = useParams();
  const project = useQuery({
    queryKey: ["projects", projectId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
  return (
    <div className="container mx-auto max-w-4xl space-y-6 p-6">
      <h1 className="text-2xl font-bold tracking-tight">Project settings</h1>
      {project.isPending && <p role="status">Loading project...</p>}
      {project.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(project.error)}
        </p>
      )}
      {project.data && (
        <>
          <RepositorySection project={project.data} />
          <AgentsSection projectId={projectId} />
          <RemoveProjectDialog project={project.data} />
        </>
      )}
    </div>
  );
}
