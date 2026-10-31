import type { components } from "@/api/schema";
import type { ApprovalCardComponent } from "../ApprovalCard";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

export type HookProposal = components["schemas"]["HookProposal"];

interface HookItemProps {
  hook: HookProposal;
}

function HookItem({ hook }: HookItemProps) {
  const { name, model, spec, why } = hook;
  const isCiDispatch = spec.type === "ci_dispatch";
  const inputsEntries = Object.entries(spec.inputs);

  return (
    <div
      data-testid={`hook-item-${name}`}
      className="space-y-2 rounded-md border border-border/70 bg-card p-3 shadow-2xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-foreground">{name}</span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
            {model ?? "project-wide"}
          </span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
            {spec.kind}
          </span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
            {spec.type}
          </span>
        </div>
        {spec.deploys && (
          <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-amber-600 dark:text-amber-400">
            deploys
          </span>
        )}
      </div>

      <div className="text-xs text-muted-foreground">
        <span className="font-medium text-foreground">Why:</span> {why}
      </div>

      <div className="space-y-1 text-xs">
        {isCiDispatch ? (
          <>
            <div>
              <span className="font-medium text-foreground">Workflow:</span>{" "}
              <code className="font-mono text-[11px] text-foreground">
                {spec.workflow ?? "none"}
              </code>
            </div>
            <div>
              <span className="font-medium text-foreground">Inputs:</span>{" "}
              <span className="text-muted-foreground">
                {inputsEntries.length === 0
                  ? "none"
                  : inputsEntries.map(([k, v]) => `${k}=${v}`).join(", ")}
              </span>
            </div>
          </>
        ) : (
          <div>
            <span className="font-medium text-foreground">Template:</span>{" "}
            <code className="font-mono text-[11px] text-foreground">{spec.template ?? "none"}</code>
          </div>
        )}

        <div>
          <span className="font-medium text-foreground">Placeholders:</span>{" "}
          <span className="text-muted-foreground">
            {spec.placeholders.length > 0 ? spec.placeholders.join(", ") : "none"}
          </span>
        </div>

        <div>
          <span className="font-medium text-foreground">Entry files:</span>{" "}
          <span className="text-muted-foreground">
            {spec.entry_files.length > 0 ? spec.entry_files.join(", ") : "none"}
          </span>
        </div>

        <div>
          <span className="font-medium text-foreground">Deploys:</span>{" "}
          <span className="text-muted-foreground">{spec.deploys ? "yes" : "no"}</span>
        </div>
      </div>
    </div>
  );
}

/**
 * Proposal card for hooks proposals: renders proposed hooks with name, model, kind, type,
 * template or workflow with its inputs, placeholders, entry files, deploys, and why.
 */
export const HooksProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

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
      {proposal && "hooks" in proposal.payload && (
        <div className="space-y-3">
          {proposal.payload.hooks.map((hook) => (
            <HookItem key={`${hook.model ?? "project"}-${hook.name}`} hook={hook} />
          ))}
        </div>
      )}
    </ProposalShell>
  );
};
