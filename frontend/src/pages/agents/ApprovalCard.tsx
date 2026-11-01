import type { ComponentType } from "react";
import { useParams } from "react-router";
import type { components } from "@/api/schema";
import { GenericApprovalCard } from "./GenericApprovalCard";
import { ConnectionsProposalCard } from "./proposals/ConnectionsProposalCard";
import { HandoffProposalCard } from "./proposals/HandoffProposalCard";
import { PromotionProposalCard } from "./proposals/PromotionProposalCard";
import { HooksProposalCard } from "./proposals/HooksProposalCard";
import { MaintenanceProposalCard } from "./proposals/MaintenanceProposalCard";
import { ParserProposalCard } from "./proposals/ParserProposalCard";
import { RuleProposalCard } from "./proposals/RuleProposalCard";
import { TrainingPolicyProposalCard } from "./proposals/TrainingPolicyProposalCard";
import { StallCard } from "./StallCard";

export type ApprovalPart = components["schemas"]["ApprovalPart"];
export type ApprovalStatus = ApprovalPart["status"];

export interface ApprovalCardProps {
  approval: ApprovalPart;
  toolName?: string;
  projectId?: string;
  className?: string;
}

/** One approval card: the dispatcher's props, with the project resolved. */
export type ApprovalCardComponent = ComponentType<
  Omit<ApprovalCardProps, "projectId"> & { projectId: string }
>;

// The card each approval kind and each proposal kind gets; one without an entry gets the
// generic card. Each plan that adds a card adds its line here, next to its component file.
export const CARD_FOR_KIND: Partial<Record<ApprovalPart["kind"], ApprovalCardComponent>> = {
  stall: StallCard,
};
export const CARD_FOR_PROPOSAL: Partial<Record<string, ApprovalCardComponent>> = {
  connections: ConnectionsProposalCard,
  handoff: HandoffProposalCard,
  promotion: PromotionProposalCard,
  hooks: HooksProposalCard,
  parser: ParserProposalCard,
  rule: RuleProposalCard,
  training_policy: TrainingPolicyProposalCard,
  maintenance: MaintenanceProposalCard,
};

/** The card an approval gets, chosen from the part alone: its kind's card, else its proposal
 * kind's card, else the generic card. */
function cardFor(approval: ApprovalPart): ApprovalCardComponent {
  const byKind = CARD_FOR_KIND[approval.kind];
  if (byKind) return byKind;
  const byProposal =
    approval.proposal_kind != null ? CARD_FOR_PROPOSAL[approval.proposal_kind] : undefined;
  return byProposal ?? GenericApprovalCard;
}

export function ApprovalCard({ projectId, ...props }: ApprovalCardProps) {
  const routeParams = useParams<{ projectId: string }>();
  const activeProjectId = projectId ?? routeParams.projectId;
  // A decision is sent to a project's route; without a project there is no card to act on.
  if (!activeProjectId) return null;
  const Card = cardFor(props.approval);
  return <Card {...props} projectId={activeProjectId} />;
}
