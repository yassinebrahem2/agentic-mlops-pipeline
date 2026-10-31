import { useParams } from "react-router";
import { errorText } from "@/lib/api";
import { useProject } from "@/lib/useProject";
import { RepositorySection } from "./RepositorySection";
import { AgentsSection } from "./AgentsSection";
import { IssuesSection } from "./IssuesSection";
import { MessagingSection } from "./MessagingSection";
import { PermissionsSection } from "./PermissionsSection";
import { PoliciesSection } from "./PoliciesSection";
import { SandboxSection } from "./SandboxSection";
import { ConnectionsSection } from "./ConnectionsSection";
import { ChecksSection } from "./ChecksSection";
import { EmbedderSection } from "./EmbedderSection";
import { HooksSection } from "./HooksSection";
import { MaintenanceSection } from "./MaintenanceSection";
import { MonitoringSection } from "./MonitoringSection";
import { OnCallSection } from "./OnCallSection";
import { TrackingSourcesSection } from "./TrackingSourcesSection";
import { RemoveProjectDialog } from "./RemoveProjectDialog";

export function ProjectSettingsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  // Every section loads and saves a project's settings; without a project there is nothing to show.
  if (!projectId) return null;
  return <ProjectSettings projectId={projectId} />;
}

function ProjectSettings({ projectId }: { projectId: string }) {
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
          <ConnectionsSection projectId={projectId} />
          <IssuesSection projectId={projectId} />
          <MessagingSection projectId={projectId} />
          <OnCallSection projectId={projectId} />
          <MonitoringSection projectId={projectId} />
          <HooksSection projectId={projectId} />
          <ChecksSection projectId={projectId} />
          <PoliciesSection projectId={projectId} />
          <MaintenanceSection projectId={projectId} />
          <EmbedderSection projectId={projectId} />
          <TrackingSourcesSection projectId={projectId} />
          <RemoveProjectDialog project={project.data} />
        </>
      )}
    </div>
  );
}
