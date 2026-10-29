import { Badge } from "@/components/ui/badge";
import type { RunStatus } from "@/lib/useTracking";

const STATUS_CONFIG: Record<
  RunStatus,
  { label: string; variant: "default" | "secondary" | "destructive" | "outline" }
> = {
  running: { label: "Running", variant: "outline" },
  succeeded: { label: "Succeeded", variant: "default" },
  failed: { label: "Failed", variant: "destructive" },
  cancelled: { label: "Cancelled", variant: "secondary" },
  other: { label: "Other", variant: "outline" },
};

export function StatusBadge({ status, className }: { status: RunStatus; className?: string }) {
  const config = STATUS_CONFIG[status];
  return (
    <Badge variant={config.variant} className={className}>
      {config.label}
    </Badge>
  );
}
