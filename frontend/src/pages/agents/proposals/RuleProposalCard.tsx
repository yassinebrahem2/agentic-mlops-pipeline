import type { components } from "@/api/schema";
import type { ApprovalCardComponent } from "../ApprovalCard";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

export type RuleProposalView = components["schemas"]["RuleProposalView"];
export type RuleSpec = components["schemas"]["RuleSpec"];
export type RuleTerm = components["schemas"]["RuleTerm"];
export type RuleLimits = components["schemas"]["RuleLimits"];
type Condition = RuleTerm["when"];

function formatCondition(c: Condition): string {
  if ("op" in c) {
    return `${c.field} ${c.op} ${String(c.value)}`;
  }
  if ("all_of" in c) {
    return `all(${c.all_of.map(formatCondition).join(", ")})`;
  }
  if ("any_of" in c) {
    return `any(${c.any_of.map(formatCondition).join(", ")})`;
  }
  if ("not_" in c) {
    return `not(${formatCondition(c.not_)})`;
  }
  return JSON.stringify(c);
}

interface RuleTermItemProps {
  term: RuleTerm;
  index: number;
}

function RuleTermItem({ term, index }: RuleTermItemProps) {
  return (
    <div
      data-testid={`rule-term-${String(index)}`}
      className="space-y-1 rounded border border-border/50 bg-background/50 p-2 text-xs"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold text-foreground">Check:</span>
        <code className="font-mono text-[11px] text-foreground">{term.check_id}</code>
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
          {term.model ?? "project-wide"}
        </span>
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
          {term.quantifier}
        </span>
      </div>
      <div>
        <span className="font-medium text-foreground">Condition:</span>{" "}
        <code className="font-mono text-[11px] text-foreground">{formatCondition(term.when)}</code>
      </div>
      <div className="flex flex-wrap items-center gap-4 text-muted-foreground">
        <div>
          <span className="font-medium text-foreground">Window:</span>{" "}
          <span>{term.window_minutes}m</span>
        </div>
        <div>
          <span className="font-medium text-foreground">Min records:</span>{" "}
          <span>{term.min_records}</span>
        </div>
      </div>
    </div>
  );
}

/**
 * Proposal card for rule proposals: renders proposed rule with name, model, why, hook,
 * limits, and monitoring terms (check, model, condition, window, quantifier, min_records).
 */
export const RuleProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

  const rulePayload =
    proposal && "spec" in proposal.payload ? (proposal.payload as { spec: RuleSpec }) : null;
  const spec = rulePayload?.spec;

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
      {spec && (
        <div className="space-y-3" data-testid="rule-proposal-details">
          <div className="space-y-2 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-foreground">{spec.name}</span>
                <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
                  {spec.model ?? "project-wide"}
                </span>
              </div>
            </div>

            <div className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">Why:</span> {spec.why}
            </div>

            <div className="space-y-1 text-xs">
              <div>
                <span className="font-medium text-foreground">Hook:</span>{" "}
                <code className="font-mono text-[11px] text-foreground">{spec.hook_id}</code>{" "}
                <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
                  {spec.hook_kind}
                </span>
              </div>

              <div>
                <span className="font-medium text-foreground">Limits:</span>{" "}
                <span className="text-muted-foreground">
                  {spec.limits.per_incident} per incident, {spec.limits.per_day} per day
                </span>
              </div>
            </div>

            <div className="space-y-2 pt-2">
              <span className="text-xs font-medium text-foreground">Terms:</span>
              <div className="space-y-1.5">
                {spec.terms.map((term, idx) => (
                  <RuleTermItem key={idx} term={term} index={idx} />
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </ProposalShell>
  );
};
