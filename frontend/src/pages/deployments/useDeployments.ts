import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";

export function deploymentsQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "deployments"] as const;
}

export function useDeployments(projectId: string | undefined) {
  useInvalidateOnEvents(projectId, deploymentsQueryKey(projectId), [
    "promotion.applied",
    "promotion.refused",
    "hook.ran",
    "pr.merged_by_agent",
  ]);

  return useQuery({
    queryKey: deploymentsQueryKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      return unwrap(
        await api.GET("/api/projects/{project_id}/deployments", {
          params: { path: { project_id: projectId } },
        }),
      );
    },
    enabled: Boolean(projectId),
  });
}
