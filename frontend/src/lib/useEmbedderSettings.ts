import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";

export type EmbedderSettingView = components["schemas"]["EmbedderSettingView"];

export function appEmbedderKey() {
  return ["embedder"] as const;
}

export function projectEmbedderKey(projectId: string | undefined) {
  return ["projects", projectId, "embedder"] as const;
}

export function useEmbedderSettings(projectId?: string) {
  const queryClient = useQueryClient();

  const queryKey = projectId ? projectEmbedderKey(projectId) : appEmbedderKey();

  const query = useQuery({
    queryKey,
    queryFn: async (): Promise<EmbedderSettingView> => {
      if (projectId) {
        return unwrap(
          await api.GET("/api/projects/{project_id}/settings/embedder", {
            params: { path: { project_id: projectId } },
          }),
        );
      }
      return unwrap(await api.GET("/api/settings/embedder"));
    },
    enabled: projectId === undefined || Boolean(projectId),
  });

  const mutation = useMutation({
    mutationFn: async (address: string | null): Promise<EmbedderSettingView> => {
      if (projectId) {
        return unwrap(
          await api.PUT("/api/projects/{project_id}/settings/embedder", {
            params: { path: { project_id: projectId } },
            body: { address },
          }),
        );
      }
      return unwrap(
        await api.PUT("/api/settings/embedder", {
          body: { address },
        }),
      );
    },
    onSuccess: (saved) => {
      if (projectId) {
        queryClient.setQueryData(projectEmbedderKey(projectId), saved);
        void queryClient.invalidateQueries({ queryKey: projectEmbedderKey(projectId) });
      } else {
        queryClient.setQueryData(appEmbedderKey(), saved);
        void queryClient.invalidateQueries({ queryKey: appEmbedderKey() });
        void queryClient.invalidateQueries({ queryKey: ["projects"] });
      }
    },
  });

  return {
    ...query,
    query,
    mutation,
  };
}
