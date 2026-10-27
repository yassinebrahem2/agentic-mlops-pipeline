import { Navigate, useOutletContext, useParams } from "react-router";
import type { PageRoute } from "@/app/routes";
import { firstProjectPath } from "./firstProjectPath";

export function ProjectHome() {
  const { projectId } = useParams<{ projectId: string }>();
  const routes = useOutletContext<PageRoute[]>();
  // Without a project there is no project page to lead to.
  if (!projectId) return null;

  const targetPath = firstProjectPath(routes, projectId);
  if (!targetPath) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-muted-foreground">
        No project pages yet.
      </div>
    );
  }

  return <Navigate to={targetPath} replace />;
}
