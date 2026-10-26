import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";
import { useProjectEvents } from "@/lib/useProjectEvents";

export type ApprovalView = components["schemas"]["ApprovalView"];

export function approvalsQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "approvals"] as const;
}

export function usePendingApprovals(projectId: string | undefined) {
  const queryClient = useQueryClient();
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const query = useQuery({
    queryKey: approvalsQueryKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/approvals", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });

  const triggerDebouncedRefetch = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      void queryClient.invalidateQueries({ queryKey: approvalsQueryKey(projectId) });
    }, 150);
  };

  useProjectEvents(projectId, {
    onEvent: (event) => {
      if (
        event.type === "approval.requested" ||
        event.type === "approval.decided" ||
        event.type === "turn.ended" ||
        event.type === "turn.waiting"
      ) {
        triggerDebouncedRefetch();
      }
    },
    onResync: triggerDebouncedRefetch,
  });

  useEffect(() => {
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);

  return query;
}

export function useInboxBadge(projectId: string | undefined): number | undefined {
  const { data } = usePendingApprovals(projectId);
  if (!data) return undefined;
  return data.length;
}
