import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import type { ProjectEventType } from "@/lib/useProjectEvents";

export type HandoffView = components["schemas"]["HandoffView"];

export function handoffKey(projectId: string | undefined) {
  return ["projects", projectId, "handoff"] as const;
}

const HANDOFF_EVENTS: readonly ProjectEventType[] = ["handoff.approved"];

export function useHandoff(projectId: string | undefined) {
  useInvalidateOnEvents(projectId, handoffKey(projectId), HANDOFF_EVENTS);

  return useQuery({
    queryKey: handoffKey(projectId),
    queryFn: async (): Promise<HandoffView | null> => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/handoff", {
        params: { path: { project_id: projectId } },
      });
      if (res.response.status === 404) {
        return null;
      }
      const data = unwrap(res);
      return data ?? null;
    },
    enabled: Boolean(projectId),
  });
}
