import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { ApprovalCardComponent, ApprovalStatus } from "./ApprovalCard";
import { ApprovalControls } from "./ApprovalControls";

// Try again approves the stall and resumes the turn; Stop, rejecting it, or the composer's
// Stop, cancelling it, end the turn.
const SETTLED_LABEL: Record<Exclude<ApprovalStatus, "pending">, string> = {
  approved: "Resumed",
  rejected: "Stopped",
  cancelled: "Stopped",
  expired: "Expired",
};

interface Row {
  label: string | null;
  detail: string;
}

/** The preview body's sentences, one row each; a "Key 1: reason." sentence is split into the
 * key and its state and reason. */
function rows(body: string): Row[] {
  return body
    .split(/\n+|(?<=\.)\s+/)
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence !== "")
    .map((sentence) => {
      const colon = sentence.indexOf(": ");
      if (colon === -1) return { label: null, detail: sentence };
      return {
        label: sentence.slice(0, colon),
        detail: sentence.slice(colon + 2).replace(/\.$/, ""),
      };
    });
}

/** A turn paused because its provider has no usable key: each key's state, Try again and Stop. */
export const StallCard: ApprovalCardComponent = ({ approval, projectId, className }) => {
  const isPending = approval.status === "pending";

  return (
    <Card
      size="sm"
      className={cn("my-2 border-amber-500/40 bg-card shadow-xs", className)}
      data-testid={`approval-card-${approval.approval_id}`}
    >
      <CardHeader className="gap-1 pb-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm font-medium text-foreground">
            {approval.preview.title}
          </CardTitle>
          {approval.status !== "pending" && (
            <Badge variant="outline" className="text-[10px] px-1.5 py-0">
              {SETTLED_LABEL[approval.status]}
            </Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-3 pt-0">
        <ul aria-label="Keys" className="space-y-1 text-xs">
          {rows(approval.preview.body).map((row, idx) => (
            <li key={idx} className="flex gap-2">
              {row.label !== null && (
                <span className="shrink-0 font-medium text-foreground">{row.label}</span>
              )}
              <span className="text-muted-foreground">{row.detail}</span>
            </li>
          ))}
        </ul>

        {isPending && (
          <ApprovalControls
            approvalId={approval.approval_id}
            projectId={projectId}
            approveLabel="Try again"
            rejectLabel="Stop"
          />
        )}
      </CardContent>
    </Card>
  );
};
