import type { ApprovalStatus } from "./ApprovalCard";

export type ApprovalBadgeStatus = ApprovalStatus | "superseded";

export const APPROVAL_STATUS_CONFIG: Record<
  ApprovalBadgeStatus,
  {
    label: string;
    variant: "default" | "secondary" | "destructive" | "outline";
    className?: string;
  }
> = {
  pending: {
    label: "pending",
    variant: "outline",
    className: "text-[10px] px-1.5 py-0",
  },
  approved: {
    label: "approved",
    variant: "outline",
    className:
      "border-emerald-500/40 text-emerald-700 dark:text-emerald-400 text-[10px] px-1.5 py-0",
  },
  rejected: {
    label: "rejected",
    variant: "destructive",
    className: "text-[10px] px-1.5 py-0",
  },
  cancelled: {
    label: "cancelled",
    variant: "outline",
    className: "text-[10px] px-1.5 py-0",
  },
  expired: {
    label: "expired",
    variant: "outline",
    className: "text-[10px] px-1.5 py-0",
  },
  superseded: {
    label: "superseded",
    variant: "outline",
    className: "text-[10px] px-1.5 py-0",
  },
};
