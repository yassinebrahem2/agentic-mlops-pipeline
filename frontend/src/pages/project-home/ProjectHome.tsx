import { Navigate, useOutletContext, useParams } from "react-router";
import { pageRoutes, type PageRoute } from "@/app/routes";
import { firstProjectPath } from "./firstProjectPath";

export function ProjectHome() {
  const { projectId } = useParams<{ projectId: string }>();
  const contextRoutes = useOutletContext<PageRoute[] | undefined>();
  const routes = contextRoutes ?? pageRoutes;

  const targetPath = firstProjectPath(routes, projectId ?? "");
  if (!targetPath) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-muted-foreground">
        No project pages yet.
      </div>
    );
  }

  return <Navigate to={targetPath} replace />;
}
