import { useParams } from "react-router";
import {
  AlertCircle,
  CircleDot,
  ExternalLink,
  GitPullRequest,
  Inbox as InboxIcon,
  Loader2,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { errorText } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "@/pages/agents/ApprovalCard";
import { LocalReviewCard } from "./LocalReviewCard";
import { useGithubActivity, useLocalReviews, usePendingApprovals } from "./usePendingApprovals";

export function InboxPage() {
  const { projectId } = useParams<{ projectId: string }>();
  // Every query and card below needs a project; without one there is nothing to ask for or show.
  if (!projectId) return null;
  return <Inbox projectId={projectId} />;
}

function Inbox({ projectId }: { projectId: string }) {
  const {
    data: approvals,
    isLoading: approvalsLoading,
    error: approvalsError,
  } = usePendingApprovals(projectId);
  const {
    data: reviews,
    isLoading: reviewsLoading,
    error: reviewsError,
  } = useLocalReviews(projectId);
  const {
    data: activity,
    isLoading: activityLoading,
    error: activityError,
  } = useGithubActivity(projectId);

  return (
    <div className="container max-w-4xl py-6 space-y-8">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <InboxIcon className="size-6 text-primary" />
          Inbox
        </h1>
        <p className="text-sm text-muted-foreground">Everything waiting on you across agents.</p>
      </div>

      {/* Section 1: Pending approvals */}
      <section aria-labelledby="approvals-heading" className="space-y-3">
        <div className="flex items-center justify-between border-b pb-2">
          <h2 id="approvals-heading" className="text-lg font-semibold tracking-tight">
            Pending approvals
          </h2>
          {approvals && approvals.length > 0 && (
            <span className="text-xs text-muted-foreground">{approvals.length} pending</span>
          )}
        </div>

        {approvalsLoading && (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Loading pending approvals...
          </div>
        )}

        {approvalsError && (
          <Alert variant="destructive">
            <AlertCircle className="size-4" />
            <AlertTitle>Error loading approvals</AlertTitle>
            <AlertDescription>{errorText(approvalsError)}</AlertDescription>
          </Alert>
        )}

        {!approvalsLoading && !approvalsError && (!approvals || approvals.length === 0) && (
          <div className="rounded-lg border border-dashed p-8 text-center">
            <p className="text-sm text-muted-foreground">No pending approvals.</p>
          </div>
        )}

        {!approvalsLoading && !approvalsError && approvals && approvals.length > 0 && (
          <div className="space-y-4">
            {approvals.map((approval) => {
              const part: ApprovalPart = {
                type: "approval",
                approval_id: approval.id,
                tool_call_id: approval.tool_call_id,
                preview: approval.preview,
                status: approval.status,
                note: approval.note,
                kind: approval.kind,
                step: approval.step,
                proposal_kind: approval.proposal_kind,
              };

              return (
                <div key={approval.id} className="space-y-1">
                  <div className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
                    <span>From agent:</span>
                    <span className="font-medium text-foreground">{approval.agent}</span>
                  </div>
                  <ApprovalCard
                    approval={part}
                    toolName={approval.tool ?? undefined}
                    projectId={projectId}
                  />
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* Section 2: Changes to merge (Local Reviews) */}
      <section aria-labelledby="reviews-heading" className="space-y-3">
        <div className="flex items-center justify-between border-b pb-2">
          <h2 id="reviews-heading" className="text-lg font-semibold tracking-tight">
            Changes to merge
          </h2>
          {reviews && reviews.length > 0 && (
            <span className="text-xs text-muted-foreground">{reviews.length} waiting</span>
          )}
        </div>

        {reviewsLoading && (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Loading changes to merge...
          </div>
        )}

        {reviewsError && (
          <Alert variant="destructive">
            <AlertCircle className="size-4" />
            <AlertTitle>Error loading local reviews</AlertTitle>
            <AlertDescription>{errorText(reviewsError)}</AlertDescription>
          </Alert>
        )}

        {!reviewsLoading && !reviewsError && (!reviews || reviews.length === 0) && (
          <div className="rounded-lg border border-dashed p-8 text-center">
            <p className="text-sm text-muted-foreground">No changes to merge.</p>
          </div>
        )}

        {!reviewsLoading && !reviewsError && reviews && reviews.length > 0 && (
          <div className="space-y-4">
            {reviews.map((review) => (
              <LocalReviewCard key={review.task.id} review={review} projectId={projectId} />
            ))}
          </div>
        )}
      </section>

      {/* Section 3: Pull requests and issues */}
      <section aria-labelledby="activity-heading" className="space-y-3">
        <div className="flex items-center justify-between border-b pb-2">
          <h2 id="activity-heading" className="text-lg font-semibold tracking-tight">
            Pull requests and issues
          </h2>
          {activity && activity.length > 0 && (
            <span className="text-xs text-muted-foreground">{activity.length} total</span>
          )}
        </div>

        {activityLoading && (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Loading pull requests and issues...
          </div>
        )}

        {activityError && (
          <Alert variant="destructive">
            <AlertCircle className="size-4" />
            <AlertTitle>Error loading activity</AlertTitle>
            <AlertDescription>{errorText(activityError)}</AlertDescription>
          </Alert>
        )}

        {!activityLoading && !activityError && (!activity || activity.length === 0) && (
          <div className="rounded-lg border border-dashed p-8 text-center">
            <p className="text-sm text-muted-foreground">No pull requests or issues.</p>
          </div>
        )}

        {!activityLoading && !activityError && activity && activity.length > 0 && (
          <div className="space-y-3">
            {activity.map((item) => (
              <div
                key={`${item.kind}-${String(item.number)}`}
                className="flex items-center justify-between rounded-lg border border-border/80 bg-card p-3 shadow-xs"
                data-testid={`activity-item-${item.kind}-${String(item.number)}`}
              >
                <div className="flex items-center gap-3 min-w-0">
                  {item.kind === "pr" ? (
                    <GitPullRequest className="size-4 shrink-0 text-primary" />
                  ) : (
                    <CircleDot className="size-4 shrink-0 text-primary" />
                  )}
                  <div className="min-w-0 space-y-0.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <a
                        href={item.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-sm font-medium hover:underline inline-flex items-center gap-1 text-foreground truncate"
                      >
                        <span>
                          #{item.number} {item.title}
                        </span>
                        <ExternalLink className="size-3 shrink-0 text-muted-foreground" />
                      </a>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Opened by <span className="font-medium text-foreground">{item.agent}</span>
                    </p>
                  </div>
                </div>
                <Badge
                  variant={item.state === "open" ? "default" : "secondary"}
                  className="text-xs capitalize shrink-0 ml-3"
                >
                  {item.state}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
