import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { ApprovalCardComponent, ApprovalStatus } from "./ApprovalCard";
import { ApprovalControls } from "./ApprovalControls";

export type ApprovalBadgeStatus = ApprovalStatus | "superseded";

export const APPROVAL_STATUS_CONFIG: Record<
  ApprovalBadgeStatus,
  {
    label: string;
    variant: "default" | "secondary" | "destructive" | "outline";
    className?: string;
  }
> = {
  pending: {
    label: "pending",
    variant: "outline",
    className: "text-[10px] px-1.5 py-0",
  },
  approved: {
    label: "approved",
    variant: "outline",
    className:
      "border-emerald-500/40 text-emerald-700 dark:text-emerald-400 text-[10px] px-1.5 py-0",
  },
  rejected: {
    label: "rejected",
    variant: "destructive",
    className: "text-[10px] px-1.5 py-0",
  },
  cancelled: {
    label: "cancelled",
    variant: "outline",
    className: "text-[10px] px-1.5 py-0",
  },
  expired: {
    label: "expired",
    variant: "outline",
    className: "text-[10px] px-1.5 py-0",
  },
  superseded: {
    label: "superseded",
    variant: "outline",
    className: "text-[10px] px-1.5 py-0",
  },
};

function DiffView({ diff }: { diff: string }) {
  const lines = diff.split("\n");

  return (
    <div
      role="region"
      aria-label="Diff view"
      className="max-h-64 overflow-x-auto overflow-y-auto rounded-md border border-border bg-muted/40 font-mono text-xs"
    >
      <table className="w-full border-collapse">
        <tbody>
          {lines.map((line, idx) => {
            let rowClass = "text-muted-foreground";
            if (line.startsWith("+") && !line.startsWith("+++")) {
              rowClass = "bg-emerald-500/15 text-emerald-800 dark:text-emerald-300";
            } else if (line.startsWith("-") && !line.startsWith("---")) {
              rowClass = "bg-rose-500/15 text-rose-800 dark:text-rose-300";
            } else if (line.startsWith("@@")) {
              rowClass = "bg-sky-500/10 text-sky-700 dark:text-sky-400 font-semibold";
            }

            const key = `${String(idx + 1)}:${line}`;
            return (
              <tr key={key} className={rowClass}>
                <td className="w-10 select-none px-2 py-0.5 text-right font-mono text-[11px] opacity-40">
                  {idx + 1}
                </td>
                <td className="whitespace-pre px-2 py-0.5 font-mono">{line || " "}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CommandView({ command }: { command: string }) {
  return (
    <pre
      role="region"
      aria-label="Command"
      className="max-h-64 overflow-x-auto rounded-md border border-border bg-muted/50 p-2.5 font-mono text-xs text-foreground"
    >
      <code>{command}</code>
    </pre>
  );
}

function TextView({ text }: { text: string }) {
  return (
    <pre
      role="region"
      aria-label="Text preview"
      className="max-h-64 overflow-x-auto whitespace-pre-wrap rounded-md border border-border bg-muted/50 p-2.5 font-mono text-xs text-foreground"
    >
      {text}
    </pre>
  );
}

/** The card of an approval no other card is registered for: its preview and the controls. */
export const GenericApprovalCard: ApprovalCardComponent = ({
  approval,
  toolName,
  projectId,
  className,
}) => {
  const isPending = approval.status === "pending";
  const statusConfig = APPROVAL_STATUS_CONFIG[approval.status];

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
              {toolName || "Approval required"}
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
        {approval.preview.kind === "diff" && <DiffView diff={approval.preview.body} />}
        {approval.preview.kind === "command" && <CommandView command={approval.preview.body} />}
        {approval.preview.kind === "text" && <TextView text={approval.preview.body} />}

        {!isPending && approval.status === "rejected" && approval.note && (
          <p className="text-xs text-muted-foreground">
            Note: <span className="text-foreground">{approval.note}</span>
          </p>
        )}

        {isPending && <ApprovalControls approvalId={approval.approval_id} projectId={projectId} />}
      </CardContent>
    </Card>
  );
};
