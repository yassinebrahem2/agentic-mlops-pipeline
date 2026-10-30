import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import type { ProjectEventType } from "@/lib/useProjectEvents";

export type ApprovalView = components["schemas"]["ApprovalView"];
export type LocalReview = components["schemas"]["LocalReview"];
export type GitHubItem = components["schemas"]["GitHubItem"];

export function approvalsQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "approvals"] as const;
}

export function reviewsQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "reviews"] as const;
}

export function activityQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "github-activity"] as const;
}

const APPROVAL_EVENTS: readonly ProjectEventType[] = [
  "approval.requested",
  "approval.decided",
  "turn.ended",
  "turn.waiting",
];

export function usePendingApprovals(projectId: string | undefined) {
  useInvalidateOnEvents(projectId, approvalsQueryKey(projectId), APPROVAL_EVENTS);

  return useQuery({
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
}

const REVIEW_EVENTS: readonly ProjectEventType[] = [
  "task.started",
  "task.ended",
  "turn.ended",
  "turn.waiting",
];

export function useLocalReviews(projectId: string | undefined) {
  useInvalidateOnEvents(projectId, reviewsQueryKey(projectId), REVIEW_EVENTS);

  return useQuery({
    queryKey: reviewsQueryKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/reviews", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

const ACTIVITY_EVENTS: readonly ProjectEventType[] = [
  "pr.opened",
  "pr.updated",
  "issue.opened",
  "issue.commented",
  "turn.ended",
];

export function useGithubActivity(projectId: string | undefined) {
  useInvalidateOnEvents(projectId, activityQueryKey(projectId), ACTIVITY_EVENTS);

  return useQuery({
    queryKey: activityQueryKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/github/activity", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}

export function useInboxBadge(projectId: string | undefined): number | undefined {
  const { data: approvals } = usePendingApprovals(projectId);
  const { data: reviews } = useLocalReviews(projectId);
  if (!approvals && !reviews) return undefined;
  const count = (approvals?.length ?? 0) + (reviews?.length ?? 0);
  return count > 0 ? count : undefined;
}
