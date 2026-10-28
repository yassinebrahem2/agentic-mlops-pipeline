import type { ComponentType } from "react";
import type { ApprovalCardProps, ApprovalPart } from "./ApprovalCard";

/** One approval card: the dispatcher's props, with the project resolved. */
export type ApprovalCardComponent = ComponentType<
  Omit<ApprovalCardProps, "projectId"> & { projectId: string }
>;

// The card each approval kind and each proposal kind gets; one without an entry gets the
// generic card. Each plan that adds a card adds its line here, next to its component file.
export const CARD_FOR_KIND: Partial<Record<ApprovalPart["kind"], ApprovalCardComponent>> = {};
export const CARD_FOR_PROPOSAL: Partial<Record<string, ApprovalCardComponent>> = {};
