import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export function projectAgentsKey(projectId: string | undefined) {
  return ["projects", projectId, "agents"] as const;
}

export function useProjectAgents(projectId: string | undefined) {
  return useQuery({
    queryKey: projectAgentsKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      return unwrap(
        await api.GET("/api/projects/{project_id}/agents", {
          params: { path: { project_id: projectId } },
        }),
      );
    },
    enabled: Boolean(projectId),
  });
}
