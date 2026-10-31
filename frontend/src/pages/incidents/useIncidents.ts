import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import { PROJECT_EVENT_TYPES, type ProjectEventType } from "@/lib/useProjectEvents";

/** UI paging sizes: how much one screen loads, not a backend bound. */
export const INCIDENT_PAGE_SIZE = 25;
export const TIMELINE_PAGE_SIZE = 25;

// One prefix for every incident query, so one invalidation refreshes list, page, signals and timeline.
export function incidentsKey(projectId: string | undefined) {
  return ["projects", projectId, "incidents"] as const;
}

// Event families that are recorded against an incident and so show up in its timeline.
const INCIDENT_EVENT_FAMILIES = ["incident.", "notification.", "escalation.", "hook.", "rule."];

const INCIDENT_EVENTS: readonly ProjectEventType[] = [
  ...PROJECT_EVENT_TYPES.filter((type) =>
    INCIDENT_EVENT_FAMILIES.some((family) => type.startsWith(family)),
  ),
  "signal.raised",
  "verdict.recorded",
];

export function useRefreshOnIncidentEvents(projectId: string | undefined): void {
  useInvalidateOnEvents(projectId, incidentsKey(projectId), INCIDENT_EVENTS);
}

export function useActiveIncidents(projectId: string | undefined) {
  return useInfiniteQuery({
    queryKey: [...incidentsKey(projectId), "list"],
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/incidents", {
        params: {
          path: { project_id: projectId },
          // `inbox` is the backend's own set: active incidents plus resolved ones awaiting a verdict.
          query: { inbox: true, limit: INCIDENT_PAGE_SIZE, offset: pageParam },
        },
      });
      return unwrap(res);
    },
    getNextPageParam: (last, pages) =>
      last.truncated ? pages.reduce((count, page) => count + page.rows.length, 0) : undefined,
    enabled: Boolean(projectId),
  });
}

export function useIncident(projectId: string | undefined, incidentId: string | undefined) {
  return useQuery({
    queryKey: [...incidentsKey(projectId), "detail", incidentId],
    queryFn: async () => {
      if (!projectId || !incidentId) throw new Error("No incident");
      const res = await api.GET("/api/projects/{project_id}/incidents/{incident_id}", {
        params: { path: { project_id: projectId, incident_id: incidentId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId && incidentId),
  });
}

export function useIncidentSignals(projectId: string, incidentId: string) {
  return useQuery({
    queryKey: [...incidentsKey(projectId), "signals", incidentId],
    queryFn: async () => {
      const res = await api.GET("/api/projects/{project_id}/incidents/{incident_id}/signals", {
        params: { path: { project_id: projectId, incident_id: incidentId } },
      });
      return unwrap(res);
    },
  });
}

export function useIncidentTimeline(projectId: string, incidentId: string) {
  return useInfiniteQuery({
    queryKey: [...incidentsKey(projectId), "timeline", incidentId],
    initialPageParam: undefined as number | undefined,
    queryFn: async ({ pageParam }) => {
      const res = await api.GET("/api/projects/{project_id}/incidents/{incident_id}/timeline", {
        params: {
          path: { project_id: projectId, incident_id: incidentId },
          query: { before: pageParam, limit: TIMELINE_PAGE_SIZE },
        },
      });
      return unwrap(res);
    },
    // The route returns no cursor: the oldest entry's seq is the next `before`, and a short page is the end.
    getNextPageParam: (last) =>
      last.length < TIMELINE_PAGE_SIZE ? undefined : last[last.length - 1]?.seq,
  });
}
