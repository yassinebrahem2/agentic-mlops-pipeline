import { useQuery } from "@tanstack/react-query";
import type { Client } from "openapi-fetch";
import { api, unwrap, type paths as GeneratedPaths } from "@/lib/api";

export type PermissionLevel = "ask" | "bypass";

export type ProjectPermissions = {
  level: PermissionLevel;
};

export type ProjectPermissionsPaths = GeneratedPaths & {
  "/api/projects/{project_id}/settings/permissions": {
    get: {
      parameters: {
        path: {
          project_id: string;
        };
      };
      responses: {
        200: {
          content: {
            "application/json": ProjectPermissions;
          };
        };
      };
    };
  };
};

export function projectPermissionsKey(projectId: string | undefined) {
  return ["projects", projectId, "settings", "permissions"] as const;
}

export function useProjectPermissions(projectId: string | undefined) {
  return useQuery({
    queryKey: projectPermissionsKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const client = api as unknown as Client<ProjectPermissionsPaths>;
      const res = await client.GET("/api/projects/{project_id}/settings/permissions", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}
