import type { ApprovalCardComponent } from "../ApprovalCard";
import { CredentialField } from "./CredentialField";
import {
  CommandsSection,
  DataRolesSection,
  GapsSection,
  GrantsSection,
  ModelsSection,
  OperationsSection,
  PipelineSection,
  TrackingSection,
} from "./HandoffSections";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

/**
 * Proposal card for handoff proposals: renders all eight payload sections in plan order,
 * credential slot inputs, warnings, and outcome results.
 */
export const HandoffProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

  const isPending = approval.status === "pending" && (!proposal || proposal.status === "pending");

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
      {proposal && "models" in proposal.payload && (
        <div className="space-y-4">
          <ModelsSection models={proposal.payload.models} />
          <CommandsSection commands={proposal.payload.commands} />
          <DataRolesSection dataRoles={proposal.payload.data_roles} />
          <TrackingSection tracking={proposal.payload.tracking} />
          <PipelineSection pipeline={proposal.payload.pipeline} edges={proposal.payload.edges} />
          <OperationsSection operations={proposal.payload.operations} />
          <GapsSection gaps={proposal.payload.gaps} />
          <GrantsSection grants={proposal.payload.grants} />

          {proposal.slots.length > 0 && (
            <section aria-label="Credentials" className="space-y-2">
              <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Credentials
              </h4>
              <div className="space-y-2">
                {proposal.slots.map((slot) => (
                  <CredentialField
                    key={slot.index}
                    projectId={projectId}
                    proposalId={proposal.id}
                    approvalId={approval.approval_id}
                    slot={slot}
                    isPending={isPending}
                  />
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </ProposalShell>
  );
};
