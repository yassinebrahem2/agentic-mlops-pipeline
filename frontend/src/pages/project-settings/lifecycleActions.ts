import type { components } from "@/api/schema";

type LifecycleStatus = components["schemas"]["HookView"]["status"];

/** The one action that moves a hook or policy in or out of service. */
export function enableLabel(status: LifecycleStatus): "Enable" | "Disable" {
  return status === "disabled" ? "Enable" : "Disable";
}
