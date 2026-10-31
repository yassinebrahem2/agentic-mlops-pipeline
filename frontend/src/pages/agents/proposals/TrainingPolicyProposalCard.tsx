import type { components } from "@/api/schema";
import type { ApprovalCardComponent } from "../ApprovalCard";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

type TrainingPolicyPayload = components["schemas"]["TrainingPolicyPayload"];

export const TrainingPolicyProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

  const payload = proposal?.payload as unknown as TrainingPolicyPayload | undefined;

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
        <div className="space-y-2.5 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
          <div className="text-xs text-muted-foreground">
            <span className="font-semibold text-foreground">Model:</span>{" "}
            <span className="font-mono text-foreground">{payload.model}</span>
          </div>

          <div className="space-y-1 text-xs">
            <div className="font-semibold text-foreground">Hooks:</div>
            <ul className="list-disc list-inside space-y-0.5 text-muted-foreground">
              <li>
                Train hook:{" "}
                <span className="font-mono text-foreground">{payload.train_hook_id}</span>
              </li>
              {payload.evaluate_hook_id && (
                <li>
                  Evaluate hook:{" "}
                  <span className="font-mono text-foreground">{payload.evaluate_hook_id}</span>
                </li>
              )}
            </ul>
          </div>

          <div className="space-y-1 text-xs">
            <div className="font-semibold text-foreground">Window Rule:</div>
            <div className="text-muted-foreground">
              {payload.window.kind === "trailing_days" && (
                <span>
                  Trailing days:{" "}
                  <span className="font-semibold text-foreground">{payload.window.days}</span> days
                </span>
              )}
              {payload.window.kind === "since_last_run" && <span>Since last run</span>}
              {payload.window.kind === "fixed" && (
                <span>
                  Fixed:{" "}
                  <span className="font-mono text-foreground">
                    {payload.window.start ?? "start"}
                  </span>{" "}
                  to{" "}
                  <span className="font-mono text-foreground">{payload.window.end ?? "end"}</span>
                </span>
              )}
            </div>
          </div>

          <div className="space-y-1 text-xs">
            <div className="font-semibold text-foreground">Limits:</div>
            <ul className="list-disc list-inside space-y-0.5 text-muted-foreground">
              <li>
                Minimum interval:{" "}
                <span className="font-semibold text-foreground">{payload.min_interval_hours}</span>{" "}
                hours
              </li>
              <li>
                Maximum runs:{" "}
                <span className="font-semibold text-foreground">{payload.max_runs_per_week}</span>{" "}
                runs/week
              </li>
              <li>
                Run-time limit:{" "}
                <span className="font-semibold text-foreground">
                  {payload.run_time_limit_minutes}
                </span>{" "}
                minutes
              </li>
            </ul>
          </div>
        </div>
      )}
    </ProposalShell>
  );
};
