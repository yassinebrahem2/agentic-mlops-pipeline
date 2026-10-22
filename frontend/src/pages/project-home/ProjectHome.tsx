import { Navigate, useParams } from "react-router";
import { pageRoutes } from "@/app/routes";
import { firstProjectPath } from "./firstProjectPath";

export function ProjectHome() {
  const { projectId } = useParams<{ projectId: string }>();

  const targetPath = firstProjectPath(pageRoutes, projectId ?? "");
  if (!targetPath) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-muted-foreground">
        No project pages yet.
      </div>
    );
  }

  return <Navigate to={targetPath} replace />;
}
