import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, errorText, unwrap } from "@/lib/api";

export interface ApprovalControlsProps {
  approvalId: string;
  projectId: string;
  approveLabel?: string;
  rejectLabel?: string;
}

/** The note field, the approve and reject buttons and the one decision request every
 * approval card sends. The card changes when message.updated brings the decision. */
export function ApprovalControls({
  approvalId,
  projectId,
  approveLabel = "Approve",
  rejectLabel = "Reject",
}: ApprovalControlsProps) {
  const [note, setNote] = useState("");
  const submittingRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDecision = async (approve: boolean) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.POST("/api/projects/{project_id}/approvals/{approval_id}/decision", {
        params: {
          path: {
            project_id: projectId,
            approval_id: approvalId,
          },
        },
        body: {
          approve,
          note: note.trim() ? note.trim() : null,
        },
      });
      unwrap(result);
    } catch (err) {
      submittingRef.current = false;
      setSubmitting(false);
      setError(errorText(err));
    }
  };

  return (
    <div className="flex flex-col gap-2 pt-1 border-t border-border/40">
      <Input
        placeholder="Optional note (for approval or rejection)..."
        value={note}
        onChange={(e) => {
          setNote(e.target.value);
        }}
        disabled={submitting}
        aria-label="Optional note"
        className="h-8 text-xs"
      />
      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          onClick={() => {
            void handleDecision(true);
          }}
          disabled={submitting}
        >
          {approveLabel}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            void handleDecision(false);
          }}
          disabled={submitting}
        >
          {rejectLabel}
        </Button>
        {error && (
          <span role="alert" className="text-xs text-destructive">
            {error}
          </span>
        )}
      </div>
    </div>
  );
}
