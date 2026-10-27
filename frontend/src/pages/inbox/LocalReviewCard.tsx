import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, errorText, unwrap } from "@/lib/api";
import { cn } from "@/lib/utils";
import { reviewsQueryKey, type LocalReview } from "./usePendingApprovals";

export interface LocalReviewCardProps {
  review: LocalReview;
  projectId: string;
  onDecided?: () => void;
  className?: string;
}

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

export function LocalReviewCard({ review, projectId, onDecided, className }: LocalReviewCardProps) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submittingRef = useRef(false);

  const handleDecision = async (approve: boolean) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.POST("/api/projects/{project_id}/reviews/{task_id}/decision", {
        params: {
          path: {
            project_id: projectId,
            task_id: review.task.id,
          },
        },
        body: {
          approve,
          note: note.trim() ? note.trim() : null,
        },
      });
      unwrap(result);
      onDecided?.();
      void queryClient.invalidateQueries({ queryKey: reviewsQueryKey(projectId) });
    } catch (err) {
      setError(errorText(err));
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <Card
      size="sm"
      className={cn("my-2 border-border/80 bg-card shadow-xs", className)}
      data-testid={`local-review-card-${review.task.id}`}
    >
      <CardHeader className="gap-1 pb-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span>From agent:</span>
            <span className="font-medium text-foreground">{review.agent}</span>
          </div>
        </div>
        <CardTitle className="text-sm font-medium text-foreground">{review.task.title}</CardTitle>
      </CardHeader>

      <CardContent className="space-y-3 pt-0">
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">
            These files in your folder will change:
          </p>
          {review.files_changed.length > 0 ? (
            <ul className="list-disc list-inside text-xs font-mono space-y-0.5 text-foreground">
              {review.files_changed.map((file) => (
                <li key={file}>{file}</li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground italic">No files changed</p>
          )}
        </div>

        {review.diff && <DiffView diff={review.diff} />}

        <div className="flex flex-col gap-2 pt-1 border-t border-border/40">
          <Input
            placeholder="Optional note for rejection..."
            value={note}
            onChange={(e) => {
              setNote(e.target.value);
            }}
            disabled={submitting}
            aria-label="Review note"
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
              Approve
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
              Reject
            </Button>
            {error && (
              <span role="alert" className="text-xs text-destructive">
                {error}
              </span>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
