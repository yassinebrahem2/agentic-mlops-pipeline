import type { components } from "@/api/schema";
import { Badge } from "@/components/ui/badge";

type LifecycleStatus = components["schemas"]["HookView"]["status"];

// Exhaustive over the generated union, so a status the backend adds fails to compile here.
const LABELS: Record<LifecycleStatus, string> = {
  active: "Active",
  suspended: "Suspended",
  disabled: "Disabled",
};

/** The status of a hook or a training policy, as text first so colour is never the only signal. */
export function LifecycleBadge({ status }: { status: LifecycleStatus }) {
  return (
    <Badge variant={status === "suspended" ? "destructive" : "secondary"}>{LABELS[status]}</Badge>
  );
}
