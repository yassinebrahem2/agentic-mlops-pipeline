import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export interface ProjectStatusProps {
  status: "ok" | "missing" | "empty" | "clone_failed";
  statusDetail?: string | null;
  status_detail?: string | null;
  className?: string;
}

export function ProjectStatus({
  status,
  statusDetail,
  status_detail,
  className,
}: ProjectStatusProps) {
  const detail = statusDetail ?? status_detail;

  if (status === "missing") {
    return (
      <div className={cn("inline-flex items-center gap-2 flex-wrap", className)}>
        <Badge variant="destructive">Folder not found</Badge>
        {detail && <span className="text-sm text-muted-foreground">{detail}</span>}
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
        {detail && <span className="text-sm text-muted-foreground">{detail}</span>}
      </div>
    );
  }

  // status === "ok"
  return (
    <div className={cn("inline-flex items-center gap-2 flex-wrap", className)}>
      <Badge
        variant="outline"
        className={
          detail
            ? "border-amber-500 text-amber-600 dark:text-amber-400"
            : "font-normal text-muted-foreground"
        }
      >
        OK
      </Badge>
      {detail && (
        <span className="text-sm text-amber-600 dark:text-amber-400" role="status">
          {detail}
        </span>
      )}
    </div>
  );
}
