import type { components } from "@/api/schema";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type ProjectStatusType = components["schemas"]["Project"]["status"];

export interface ProjectStatusProps {
  status: ProjectStatusType;
  status_detail?: string | null;
  className?: string;
}

export function ProjectStatus({ status, status_detail, className }: ProjectStatusProps) {
  if (status === "missing") {
    return (
      <div className={cn("inline-flex items-center gap-2 flex-wrap", className)}>
        <Badge variant="destructive">Folder not found</Badge>
        {status_detail && <span className="text-sm text-muted-foreground">{status_detail}</span>}
      </div>
    );
  }

  if (status === "empty") {
    return (
      <div className={cn("inline-flex items-center gap-2 flex-wrap", className)}>
        <Badge variant="secondary">Empty</Badge>
        <span className="text-sm text-muted-foreground">
          No committed files yet. Commit your files first.
        </span>
      </div>
    );
  }

  if (status === "clone_failed") {
    return (
      <div className={cn("inline-flex items-center gap-2 flex-wrap", className)}>
        <Badge variant="destructive">Download failed</Badge>
        {status_detail && <span className="text-sm text-muted-foreground">{status_detail}</span>}
      </div>
    );
  }

  return (
    <div className={cn("inline-flex items-center gap-2 flex-wrap", className)}>
      <Badge
        variant="outline"
        className={
          status_detail
            ? "border-amber-500 text-amber-600 dark:text-amber-400"
            : "font-normal text-muted-foreground"
        }
      >
        OK
      </Badge>
      {status_detail && (
        <span className="text-sm text-amber-600 dark:text-amber-400" role="status">
          {status_detail}
        </span>
      )}
    </div>
  );
}
