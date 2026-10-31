import { AlertTriangle, CheckCircle2, Clock, HelpCircle, type LucideIcon } from "lucide-react";
import type { components } from "@/api/schema";
import { cn } from "@/lib/utils";

type StageStatus = components["schemas"]["StageStatus"];

// Display only: each status has its own icon and word next to its colour, so colour is never
// the only signal.
const STATUS_DISPLAY: Record<
  StageStatus["status"],
  { label: string; icon: LucideIcon; tone: string }
> = {
  ok: { label: "OK", icon: CheckCircle2, tone: "text-emerald-700 dark:text-emerald-400" },
  late: { label: "Late", icon: Clock, tone: "text-amber-700 dark:text-amber-400" },
  incident: { label: "Incident", icon: AlertTriangle, tone: "text-destructive" },
  unknown: { label: "Unknown", icon: HelpCircle, tone: "text-muted-foreground" },
};

export function StageCard({ stage }: { stage: StageStatus }) {
  const display = STATUS_DISPLAY[stage.status];
  const Icon = display.icon;
  const count = stage.incident_ids.length;
  return (
    <li
      data-testid={`stage-${stage.name}`}
      className={cn(
        "min-w-40 rounded-lg border bg-card p-3 text-sm shadow-xs",
        stage.status === "incident" && "border-destructive/60",
      )}
    >
      <p className="font-medium break-words">{stage.name}</p>
      <p className={cn("mt-1 flex items-center gap-1.5 font-medium", display.tone)}>
        <Icon className="size-4" aria-hidden="true" />
        {display.label}
      </p>
      {count > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          {count} active {count === 1 ? "incident" : "incidents"}
        </p>
      )}
    </li>
  );
}
