import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";
import { incidentsKey } from "./useIncidents";
import type { ResultCategory, VerdictDraft } from "./labels";

type Target = { projectId: string; incidentId: string };

function useIncidentMutation<V>(projectId: string, run: (variables: V) => Promise<unknown>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: run,
    // Right away, not through the event debounce: the person just acted and expects the row to change.
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: incidentsKey(projectId) });
    },
  });
}

export function useAcknowledge({ projectId, incidentId }: Target) {
  return useIncidentMutation(projectId, async () =>
    unwrap(
      await api.POST("/api/projects/{project_id}/incidents/{incident_id}/acknowledge", {
        params: { path: { project_id: projectId, incident_id: incidentId } },
      }),
    ),
  );
}

export function useCloseIncident({ projectId, incidentId }: Target) {
  return useIncidentMutation(projectId, async () =>
    unwrap(
      await api.POST("/api/projects/{project_id}/incidents/{incident_id}/close", {
        params: { path: { project_id: projectId, incident_id: incidentId } },
      }),
    ),
  );
}

export function useSnooze({ projectId, incidentId }: Target) {
  return useIncidentMutation(projectId, async (until: Date) =>
    unwrap(
      await api.POST("/api/projects/{project_id}/incidents/{incident_id}/snooze", {
        params: { path: { project_id: projectId, incident_id: incidentId } },
        body: { until: until.toISOString() },
      }),
    ),
  );
}

export function useRecordVerdict({ projectId, incidentId }: Target) {
  return useIncidentMutation(projectId, async (body: VerdictDraft) =>
    unwrap(
      await api.POST("/api/projects/{project_id}/incidents/{incident_id}/verdict", {
        params: { path: { project_id: projectId, incident_id: incidentId } },
        body,
      }),
    ),
  );
}

export type MissInput = {
  description: string;
  category: ResultCategory | undefined;
  occurredAt: Date;
};

export function useReportMiss(projectId: string) {
  return useMutation({
    mutationFn: async ({ description, category, occurredAt }: MissInput) => {
      unwrap(
        await api.POST("/api/projects/{project_id}/misses", {
          params: { path: { project_id: projectId } },
          body: {
            description,
            ...(category ? { category } : {}),
            occurred_at: occurredAt.toISOString(),
          },
        }),
      );
    },
  });
}
