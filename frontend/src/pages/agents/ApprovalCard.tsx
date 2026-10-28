import { useParams } from "react-router";
import type { components } from "@/api/schema";
import { CARD_FOR_KIND, CARD_FOR_PROPOSAL, type ApprovalCardComponent } from "./approvalCards";
import { GenericApprovalCard } from "./GenericApprovalCard";

export type ApprovalPart = components["schemas"]["ApprovalPart"];
export type ApprovalStatus = ApprovalPart["status"];

export interface ApprovalCardProps {
  approval: ApprovalPart;
  toolName?: string;
  projectId?: string;
  className?: string;
}

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
