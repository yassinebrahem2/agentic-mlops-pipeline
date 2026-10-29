import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";

export type ProposalView = components["schemas"]["ProposalView"];

export function proposalByApprovalKey(
  projectId: string | undefined,
  approvalId: string | undefined,
) {
  return ["projects", projectId, "proposals", "by-approval", approvalId] as const;
}

export function useProposal(projectId: string | undefined, approvalId: string | undefined) {
  return useQuery({
    queryKey: proposalByApprovalKey(projectId, approvalId),
    queryFn: async () => {
      if (!projectId || !approvalId) {
        throw new Error("Missing projectId or approvalId");
      }
      return unwrap(
        await api.GET("/api/projects/{project_id}/proposals/by-approval/{approval_id}", {
          params: {
            path: {
              project_id: projectId,
              approval_id: approvalId,
            },
          },
        }),
      );
    },
    enabled: Boolean(projectId && approvalId),
  });
}
