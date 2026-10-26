import { useParams } from "react-router";
import { errorText } from "@/lib/api";
import { useProject } from "@/lib/useProject";
import { RepositorySection } from "./RepositorySection";
import { AgentsSection } from "./AgentsSection";
import { IssuesSection } from "./IssuesSection";
import { PermissionsSection } from "./PermissionsSection";
import { SandboxSection } from "./SandboxSection";
import { RemoveProjectDialog } from "./RemoveProjectDialog";

export function ProjectSettingsPage() {
  const { projectId = "" } = useParams();
  const project = useProject(projectId);
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
          <PermissionsSection projectId={projectId} />
          <SandboxSection projectId={projectId} />
          <IssuesSection projectId={projectId} />
          <RemoveProjectDialog project={project.data} />
        </>
      )}
    </div>
  );
}
