import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import type { ProjectEventType } from "@/lib/useProjectEvents";

export const RUNS_PAGE_LIMIT = 200;
export const VERSIONS_PAGE_LIMIT = 100;

export type TrackingSourceView = components["schemas"]["TrackingSourceView"];
export type ModelSummary = components["schemas"]["ModelSummary"];
export type ModelVersionRow = components["schemas"]["ModelVersionRow"];
export type RunRow = components["schemas"]["RunRow"];
export type CiRunRow = components["schemas"]["CiRunRow"];
export type DeploymentRow = components["schemas"]["DeploymentRow"];
export type MetricPoint = components["schemas"]["MetricPoint"];
export type SourceType = components["schemas"]["TrackingSourceView"]["type"];
export type RunStatus = components["schemas"]["RunRow"]["status"];
export type Page_ModelSummary_ = components["schemas"]["Page_ModelSummary_"];
export type Page_ModelVersionRow_ = components["schemas"]["Page_ModelVersionRow_"];
export type Page_RunRow_ = components["schemas"]["Page_RunRow_"];
export type Page_CiRunRow_ = components["schemas"]["Page_CiRunRow_"];
export type Page_DeploymentRow_ = components["schemas"]["Page_DeploymentRow_"];
export type Page_MetricPoint_ = components["schemas"]["Page_MetricPoint_"];
export type CreateSourceBody = components["schemas"]["CreateSourceBody"];
export type SetCredentialBody = components["schemas"]["SetCredentialBody"];

export type TrackingPart =
  "sources" | "models" | "versions" | "runs" | "ci-runs" | "deployments" | "metrics";

export function trackingPrefix(projectId: string | undefined) {
  return ["projects", projectId, "tracking"] as const;
}

export function trackingKey(projectId: string | undefined, part: TrackingPart) {
  return ["projects", projectId, "tracking", part] as const;
}

const TRACKING_EVENTS: readonly ProjectEventType[] = ["tracking.synced"];

export function useTrackingSources(projectId: string | undefined) {
  useInvalidateOnEvents(projectId, trackingPrefix(projectId), TRACKING_EVENTS);

  return useQuery({
    queryKey: trackingKey(projectId, "sources"),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/tracking/sources", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

export function useTrackedModels(
  projectId: string | undefined,
  params?: { limit?: number; offset?: number },
) {
  useInvalidateOnEvents(projectId, trackingPrefix(projectId), TRACKING_EVENTS);

  return useQuery({
    queryKey: [...trackingKey(projectId, "models"), params] as const,
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/tracking/models", {
        params: { path: { project_id: projectId }, query: params },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

export function useModelVersions(
  projectId: string | undefined,
  sourceId?: string | null,
  model?: string | null,
  params?: { limit?: number; offset?: number },
) {
  useInvalidateOnEvents(projectId, trackingPrefix(projectId), TRACKING_EVENTS);

  return useQuery({
    queryKey: [...trackingKey(projectId, "versions"), sourceId, model, params] as const,
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/tracking/versions", {
        params: {
          path: { project_id: projectId },
          query: {
            source_id: sourceId ?? undefined,
            model: model ?? undefined,
            limit: params?.limit,
            offset: params?.offset,
          },
        },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

export function useTrackedRuns(
  projectId: string | undefined,
  params?: { limit?: number; offset?: number },
) {
  useInvalidateOnEvents(projectId, trackingPrefix(projectId), TRACKING_EVENTS);

  return useQuery({
    queryKey: [...trackingKey(projectId, "runs"), params] as const,
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/tracking/runs", {
        params: { path: { project_id: projectId }, query: params },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

export function useRunByKey(
  projectId: string | undefined,
  runKey: string | null | undefined,
  enabled = true,
) {
  useInvalidateOnEvents(projectId, trackingPrefix(projectId), TRACKING_EVENTS);

  return useQuery({
    queryKey: [...trackingKey(projectId, "runs"), { run_key: runKey }] as const,
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      if (!runKey) return null;
      const res = await api.GET("/api/projects/{project_id}/tracking/runs", {
        params: { path: { project_id: projectId }, query: { run_key: runKey } },
      });
      const page = unwrap(res);
      return page.rows[0] ?? null;
    },
    enabled: Boolean(projectId && runKey && enabled),
  });
}

export function useCiRuns(
  projectId: string | undefined,
  params?: { limit?: number; offset?: number },
) {
  useInvalidateOnEvents(projectId, trackingPrefix(projectId), TRACKING_EVENTS);

  return useQuery({
    queryKey: [...trackingKey(projectId, "ci-runs"), params] as const,
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/tracking/ci-runs", {
        params: { path: { project_id: projectId }, query: params },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

export function useDeployments(
  projectId: string | undefined,
  sourceId?: string | null,
  model?: string | null,
  params?: { limit?: number; offset?: number },
) {
  useInvalidateOnEvents(projectId, trackingPrefix(projectId), TRACKING_EVENTS);

  return useQuery({
    queryKey: [...trackingKey(projectId, "deployments"), sourceId, model, params] as const,
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/tracking/deployments", {
        params: {
          path: { project_id: projectId },
          query: {
            source_id: sourceId ?? undefined,
            model: model ?? undefined,
            limit: params?.limit,
            offset: params?.offset,
          },
        },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

export function useRunMetrics(
  projectId: string | undefined,
  runKey: string | null | undefined,
  params?: { limit?: number; offset?: number },
) {
  useInvalidateOnEvents(projectId, trackingPrefix(projectId), TRACKING_EVENTS);

  return useQuery({
    queryKey: [...trackingKey(projectId, "metrics"), runKey, params] as const,
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/tracking/metrics", {
        params: {
          path: { project_id: projectId },
          query: {
            run_key: runKey ?? undefined,
            latest: true,
            limit: params?.limit,
            offset: params?.offset,
          },
        },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId && runKey),
  });
}

export function useSyncNow(projectId: string | undefined) {
  return useMutation({
    mutationFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.POST("/api/projects/{project_id}/tracking/sync", {
        params: { path: { project_id: projectId } },
      });
      unwrap(res);
    },
  });
}

export function useCreateSource(projectId: string | undefined) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (body: CreateSourceBody) => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.POST("/api/projects/{project_id}/tracking/sources", {
        params: { path: { project_id: projectId } },
        body,
      });
      return unwrap(res);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: trackingKey(projectId, "sources") });
    },
  });
}

export function useSetSourceCredential(projectId: string | undefined) {
  const queryClient = useQueryClient();

  return useMutation({
    gcTime: 0,
    mutationFn: async ({ sourceId, credential }: { sourceId: string; credential: string }) => {
      if (!projectId) throw new Error("No projectId");
      try {
        const res = await api.PUT(
          "/api/projects/{project_id}/tracking/sources/{source_id}/credential",
          {
            params: { path: { project_id: projectId, source_id: sourceId } },
            body: { credential },
          },
        );
        return unwrap(res);
      } catch (err: unknown) {
        if (err instanceof Error) {
          const sanitized = err.message.split(credential).join("********");
          throw new Error(sanitized);
        }
        throw err;
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: trackingKey(projectId, "sources") });
    },
    onSettled: (_data, _error, variables) => {
      delete (variables as Partial<typeof variables>).credential;
      for (const mut of queryClient.getMutationCache().getAll()) {
        const vars = mut.state.variables as Record<string, unknown> | undefined;
        if (vars && "credential" in vars) {
          delete vars.credential;
          queryClient.getMutationCache().remove(mut);
        }
      }
    },
  });
}
