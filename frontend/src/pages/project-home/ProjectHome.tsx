import { Navigate, useParams } from "react-router";
import { pageRoutes, type PageRoute } from "@/app/routes";
import { firstProjectPath, getProjectHomeRoutes } from "./firstProjectPath";

export function ProjectHome({ routes }: { routes?: PageRoute[] }) {
  const { projectId } = useParams<{ projectId: string }>();
  const activeRoutes = routes ?? getProjectHomeRoutes(pageRoutes);

  if (!projectId) {
    return <Navigate to="/" replace />;
  }

  const targetPath = firstProjectPath(activeRoutes, projectId);
  if (!targetPath) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-muted-foreground">
        No project pages yet.
      </div>
    );
  }

  return <Navigate to={targetPath} replace />;
}
