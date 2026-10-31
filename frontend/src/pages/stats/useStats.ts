import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";
import type { ResultCategory, StatsWindow } from "./metricLabels";

export type SilentVerdict = components["schemas"]["SilentVerdictBody"]["value"];
export type CategoryChoice = ResultCategory | "all";

export function useStats(projectId: string, statsWindow: StatsWindow, category: CategoryChoice) {
  return useQuery({
    queryKey: ["stats", projectId, statsWindow, category],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/stats", {
          params: {
            path: { project_id: projectId },
            query: category === "all" ? { window: statsWindow } : { window: statsWindow, category },
          },
        }),
      ),
  });
}

export function useSilentAlerts(projectId: string) {
  return useQuery({
    queryKey: ["silent-alerts", projectId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/baseline/silent-alerts", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
}

export function useJudgeSilentAlert(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ resultId, value }: { resultId: string; value: SilentVerdict }) =>
      unwrap(
        await api.POST("/api/projects/{project_id}/baseline/silent-alerts/{result_id}/verdict", {
          params: { path: { project_id: projectId, result_id: resultId } },
          body: { value },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["silent-alerts", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["stats", projectId] }),
      ]);
    },
  });
}
