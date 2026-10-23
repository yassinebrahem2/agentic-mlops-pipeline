import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export function projectKey(projectId: string | undefined) {
  return ["projects", projectId] as const;
}

export function useProject(projectId: string | undefined) {
  return useQuery({
    queryKey: projectKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      return unwrap(
        await api.GET("/api/projects/{project_id}", {
          params: { path: { project_id: projectId } },
        }),
      );
    },
    enabled: Boolean(projectId),
  });
}
