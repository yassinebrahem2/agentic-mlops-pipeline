import { useQuery } from "@tanstack/react-query";
import { ReportDetails } from "@/components/ReportDetails";
import { api, unwrap } from "@/lib/api";
import type { ApprovalCardComponent } from "../ApprovalCard";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

export const PromotionProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

  const payload = proposal?.kind === "promotion" ? proposal.payload : undefined;
  const reportId = payload?.report_id;

  const { data: report } = useQuery({
    queryKey: ["projects", projectId, "reports", reportId],
    queryFn: async () => {
      if (!reportId) {
        throw new Error("Missing reportId");
      }
      return unwrap(
        await api.GET("/api/projects/{project_id}/reports/{report_id}", {
          params: { path: { project_id: projectId, report_id: reportId } },
        }),
      );
    },
    enabled: Boolean(reportId),
  });

  return (
    <ProposalShell
      approval={approval}
      proposal={proposal}
      isLoading={isLoading}
      isError={isError}
      projectId={projectId}
      toolName={toolName}
      className={className}
    >
      {payload && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <span className="text-muted-foreground">Model:</span>{" "}
              <span className="font-semibold text-foreground">{payload.model}</span>
            </div>
            <div>
              <span className="text-muted-foreground">Candidate Version:</span>{" "}
              <span className="font-semibold text-foreground">{payload.candidate_version}</span>
            </div>
            <div>
              <span className="text-muted-foreground">Champion Version:</span>{" "}
              <span className="font-semibold text-foreground">
                {payload.champion_version ?? "None"}
              </span>
            </div>
            <div>
              <span className="text-muted-foreground">Deploy Hook:</span>{" "}
              <span className="font-mono text-foreground">{payload.deploy_hook_id}</span>
            </div>
          </div>

          {report && <ReportDetails report={report} />}
        </div>
      )}
    </ProposalShell>
  );
};
