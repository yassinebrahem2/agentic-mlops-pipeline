import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { ApprovalPart } from "../ApprovalCard";
import { ApprovalControls } from "../ApprovalControls";
import { APPROVAL_STATUS_CONFIG, type ApprovalBadgeStatus } from "../approvalStatus";
import type { ProposalView } from "./useProposal";

export interface ProposalShellProps {
  approval: ApprovalPart;
  proposal?: ProposalView | null;
  isLoading?: boolean;
  isError?: boolean;
  toolName?: string;
  projectId: string;
  className?: string;
  children?: ReactNode;
}

/**
 * Shared container for proposal cards: title, status badge, loading and error fallback
 * showing preview.body text, result lines, warnings as given, and ApprovalControls while pending.
 */
export function ProposalShell({
  approval,
  proposal,
  isLoading = false,
  isError = false,
  toolName,
  projectId,
  className,
  children,
}: ProposalShellProps) {
  const isPending = approval.status === "pending" && (!proposal || proposal.status === "pending");

  const displayStatus: ApprovalBadgeStatus =
    proposal?.status === "superseded"
      ? "superseded"
      : proposal?.status && proposal.status !== "pending"
        ? proposal.status
        : approval.status;

  const statusConfig = APPROVAL_STATUS_CONFIG[displayStatus];

  const showFallback = isLoading || isError || !proposal;

  return (
    <Card
      size="sm"
      className={cn("my-2 border-border/80 bg-card shadow-xs", className)}
      data-testid={`approval-card-${approval.approval_id}`}
    >
      <CardHeader className="gap-1 pb-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {toolName || "Proposal"}
            </span>
            {!isPending && (
              <Badge variant={statusConfig.variant} className={statusConfig.className}>
                {statusConfig.label}
              </Badge>
            )}
          </div>
        </div>
        <CardTitle className="text-sm font-medium text-foreground">
          {approval.preview.title}
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-3 pt-0">
        {showFallback ? (
          <pre
            role="region"
            aria-label="Text preview"
            className="max-h-64 overflow-x-auto whitespace-pre-wrap rounded-md border border-border bg-muted/50 p-2.5 font-mono text-xs text-foreground"
          >
            {approval.preview.body}
          </pre>
        ) : (
          children
        )}

        {proposal?.result && (
          <div className="space-y-2 rounded-md border border-border/60 bg-muted/30 p-2.5 text-xs">
            {proposal.result.applied.length > 0 && (
              <div className="text-muted-foreground">
                <span className="font-semibold text-foreground">Applied:</span>{" "}
                <span>{proposal.result.applied.join(", ")}</span>
              </div>
            )}
            {Object.keys(proposal.result.failed).length > 0 && (
              <div className="space-y-1">
                <span className="font-semibold text-destructive">Failed:</span>
                <ul className="list-disc list-inside space-y-0.5 text-muted-foreground">
                  {Object.entries(proposal.result.failed).map(([key, reason]) => (
                    <li key={key}>
                      <span className="font-semibold text-foreground">{key}</span>: {reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {!isPending && approval.status === "rejected" && approval.note && (
          <p className="text-xs text-muted-foreground">
            Note: <span className="text-foreground">{approval.note}</span>
          </p>
        )}

        {proposal?.warnings && proposal.warnings.length > 0 && (
          <div className="space-y-1 text-xs text-amber-600 dark:text-amber-400">
            {proposal.warnings.map((warning, idx) => (
              <p key={idx}>{warning}</p>
            ))}
          </div>
        )}

        {isPending && <ApprovalControls approvalId={approval.approval_id} projectId={projectId} />}
      </CardContent>
    </Card>
  );
}
