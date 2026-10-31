import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import { PROJECT_EVENT_TYPES, type ProjectEventType } from "@/lib/useProjectEvents";

export type Dashboard = components["schemas"]["DashboardView"];

// What the Dashboard shows changes with incidents and their signals, collected results, a new
// handoff (stages, models), tracker syncs (production versions) and what the agents do.
const REFRESH_ON: readonly ProjectEventType[] = PROJECT_EVENT_TYPES.filter(
  (type) =>
    type.startsWith("incident.") ||
    type.startsWith("turn.") ||
    type.startsWith("task.") ||
    type === "signal.raised" ||
    type === "results.collected" ||
    type === "handoff.approved" ||
    type === "tracking.synced",
);

export function dashboardQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "dashboard"] as const;
}

export function useDashboard(projectId: string | undefined) {
  useInvalidateOnEvents(projectId, dashboardQueryKey(projectId), REFRESH_ON);

  return useQuery({
    queryKey: dashboardQueryKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/dashboard", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}
