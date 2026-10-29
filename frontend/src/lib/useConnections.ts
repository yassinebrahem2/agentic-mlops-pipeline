import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import type { ProjectEventType } from "@/lib/useProjectEvents";

export type ConnectionView = components["schemas"]["ConnectionView"];
export type TableRef = components["schemas"]["TableRef"];

export function connectionsKey(projectId: string | undefined) {
  return ["projects", projectId, "connections"] as const;
}

export function tablesKey(projectId: string | undefined, connectionId: string | undefined) {
  return ["projects", projectId, "connections", connectionId, "tables"] as const;
}

const CONNECTION_EVENTS: readonly ProjectEventType[] = [
  "connection.created",
  "connection.updated",
  "connection.deleted",
];

export function useConnections(projectId: string | undefined) {
  useInvalidateOnEvents(projectId, connectionsKey(projectId), CONNECTION_EVENTS);

  return useQuery({
    queryKey: connectionsKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/connections", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

export function useTables(projectId: string | undefined, connectionId: string | undefined) {
  useInvalidateOnEvents(projectId, connectionsKey(projectId), CONNECTION_EVENTS);

  return useQuery({
    queryKey: tablesKey(projectId, connectionId),
    queryFn: async () => {
      if (!projectId || !connectionId) throw new Error("No projectId or connectionId");
      const res = await api.GET("/api/projects/{project_id}/connections/{connection_id}/tables", {
        params: { path: { project_id: projectId, connection_id: connectionId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId && connectionId),
  });
}
