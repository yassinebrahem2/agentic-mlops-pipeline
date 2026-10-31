import { Badge } from "@/components/ui/badge";
import type { ApprovalCardComponent } from "../ApprovalCard";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

/**
 * Proposal card for maintenance window proposals: renders window start/end time,
 * scope badges, and reason, with standard approval controls via ProposalShell.
 */
export const MaintenanceProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

  const window = proposal?.kind === "maintenance" ? proposal.payload.window : null;

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
      {window && (
        <div className="space-y-3 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <span className="font-medium text-muted-foreground">Start:</span>{" "}
              <span className="font-mono text-foreground">{window.start}</span>
            </div>
            <div>
              <span className="font-medium text-muted-foreground">End:</span>{" "}
              <span className="font-mono text-foreground">{window.end}</span>
            </div>
          </div>

          <div className="space-y-1">
            <div className="text-xs font-medium text-muted-foreground">Scope:</div>
            <div className="flex flex-wrap gap-1">
              {window.scope.map((s) => (
                <Badge key={s} variant="outline" className="text-xs font-mono">
                  {s}
                </Badge>
              ))}
            </div>
          </div>

          <div className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">Reason:</span> {window.reason}
          </div>
        </div>
      )}
    </ProposalShell>
  );
};
