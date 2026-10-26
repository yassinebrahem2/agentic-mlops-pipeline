import { useParams } from "react-router";
import { AlertCircle, Inbox as InboxIcon, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ApprovalCard, type ApprovalPart } from "@/pages/agents/ApprovalCard";
import { usePendingApprovals } from "./usePendingApprovals";

export function InboxPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const { data: approvals, isLoading, error } = usePendingApprovals(projectId);

  return (
    <div className="container max-w-4xl py-6 space-y-6">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <InboxIcon className="size-6 text-primary" />
          Inbox
        </h1>
        <p className="text-sm text-muted-foreground">Everything waiting on you across agents.</p>
      </div>

      <section aria-labelledby="approvals-heading" className="space-y-3">
        <div className="flex items-center justify-between border-b pb-2">
          <h2 id="approvals-heading" className="text-lg font-semibold tracking-tight">
            Pending approvals
          </h2>
          {approvals && approvals.length > 0 && (
            <span className="text-xs text-muted-foreground">{approvals.length} pending</span>
          )}
        </div>

        {isLoading && (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Loading pending approvals...
          </div>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertCircle className="size-4" />
            <AlertTitle>Error loading approvals</AlertTitle>
            <AlertDescription>
              {error instanceof Error ? error.message : "Failed to load approvals"}
            </AlertDescription>
          </Alert>
        )}

        {!isLoading && !error && (!approvals || approvals.length === 0) && (
          <div className="rounded-lg border border-dashed p-8 text-center">
            <p className="text-sm text-muted-foreground">No pending approvals.</p>
          </div>
        )}

        {!isLoading && !error && approvals && approvals.length > 0 && (
          <div className="space-y-4">
            {approvals.map((approval) => {
              const part: ApprovalPart = {
                type: "approval",
                approval_id: approval.id,
                tool_call_id: approval.tool_call_id,
                preview: approval.preview,
                status: approval.status,
                note: approval.note,
              };

              return (
                <div key={approval.id} className="space-y-1">
                  <div className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
                    <span>From agent:</span>
                    <span className="font-medium text-foreground">{approval.agent}</span>
                  </div>
                  <ApprovalCard approval={part} toolName={approval.tool} projectId={projectId} />
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
