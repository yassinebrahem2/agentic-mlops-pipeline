import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";

export type ProjectPermissions = components["schemas"]["PermissionsSettings"];
export type PermissionLevel = ProjectPermissions["level"];

export function projectPermissionsKey(projectId: string | undefined) {
  return ["projects", projectId, "settings", "permissions"] as const;
}

export function useProjectPermissions(projectId: string | undefined) {
  return useQuery({
    queryKey: projectPermissionsKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/settings/permissions", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}
