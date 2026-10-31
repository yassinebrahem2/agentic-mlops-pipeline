import type { components } from "@/api/schema";
import type { ProjectEventType } from "@/lib/useProjectEvents";

type Schemas = components["schemas"];
export type IncidentView = Schemas["IncidentView"];
export type SignalView = Schemas["SignalView"];
export type VerdictView = Schemas["VerdictView"];
export type VerdictDraft = Schemas["VerdictDraft"];
export type ResultCategory = IncidentView["category"];
export type Severity = IncidentView["severity"];
export type IncidentStatus = IncidentView["status"];
export type VerdictValue = VerdictDraft["value"];

// The Records are exhaustive over the generated unions, so a value the backend adds fails to
// compile here until it has a label; the keys are then the only list the pickers need.
export const CATEGORY_LABELS: Record<ResultCategory, string> = {
  data_quality: "Data quality",
  drift: "Drift",
  model_performance: "Model performance",
  operational: "Operational",
};
export const CATEGORIES = Object.keys(CATEGORY_LABELS) as ResultCategory[];

export const SEVERITY_LABELS: Record<Severity, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

export const STATUS_LABELS: Record<IncidentStatus, string> = {
  open: "Open",
  acknowledged: "Acknowledged",
  mitigated: "Mitigated",
  resolved: "Resolved",
  closed: "Closed",
  reopened: "Reopened",
  merged: "Merged",
};

export const VERDICT_LABELS: Record<VerdictValue, string> = {
  real_issue: "Real issue",
  not_an_issue: "Not an issue",
  wrong_type: "Wrong type",
};

// Partial on purpose: the timeline shows every event of the incident, including types this
// page has no wording for yet, and eventLabel falls back to the raw type for those.
const EVENT_LABELS: Partial<Record<ProjectEventType, string>> = {
  "signal.raised": "Signal raised",
  "incident.opened": "Incident opened",
  "incident.updated": "Incident updated",
  "incident.acknowledged": "Incident acknowledged",
  "incident.snoozed": "Incident snoozed",
  "incident.severity_changed": "Severity changed",
  "incident.reopened": "Incident reopened",
  "incident.merged": "Incident merged",
  "incident.split": "Incident split",
  "incident.suspect_linked": "Suspect change linked",
  "incident.hold_recorded": "Hold recorded",
  "incident.wake_skipped": "Agent wake skipped",
  "incident.closed": "Incident closed",
  "notification.sent": "Notification sent",
  "notification.failed": "Notification failed",
  "escalation.sent": "Escalation sent",
  "hook.ran": "Hook ran",
  "hook.failed": "Hook failed",
  "hook.suspended": "Hook suspended",
  "rule.satisfied": "Rule satisfied",
  "verdict.recorded": "Verdict recorded",
  "miss.reported": "Miss reported",
  "fix.requested": "Fix requested",
  "fix.chosen": "Fix chosen",
  "fix.declined": "Fix declined",
  "fix.escalated": "Fix escalated",
  "training.requested": "Training requested",
  "training.linked": "Training linked",
  "policy.suspended": "Policy suspended",
};

export function eventLabel(type: string): string {
  return (EVENT_LABELS as Partial<Record<string, string>>)[type] ?? type;
}
