import type { components } from "@/api/schema";

type Decision = components["schemas"]["DecisionRow"];

// Records keyed by the generated unions, so a kind or decider the backend adds fails the type check here.
export const KIND_LABEL: Record<Decision["kind"], string> = {
  promotion: "Promotion",
  rollback: "Rollback",
  fall_back: "Fall-back",
};

const DECIDER_LABEL: Record<Decision["decided_by"], string> = {
  person: "Person",
  team_gate: "Team gate",
  rule: "Rule",
  agent_bypass: "Agent bypass",
};

/** The ref is a rule ID for a rule and an approval ID for a person (the approval records who decided). */
export function deciderLabel(decidedBy: Decision["decided_by"], ref: string | null | undefined) {
  const label = DECIDER_LABEL[decidedBy];
  if (!ref) return label;
  return decidedBy === "person" ? `${label} (approval ${ref})` : `${label} ${ref}`;
}

export function versionChange(from: string | null | undefined, to: string | null | undefined) {
  return `${from ?? "unknown"} → ${to ?? "unknown"}`;
}
