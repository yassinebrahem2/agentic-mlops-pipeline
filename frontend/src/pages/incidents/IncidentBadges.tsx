import { Link } from "react-router";
import { Badge } from "@/components/ui/badge";
import { formatTime } from "@/lib/format";
import { relativeAge } from "./age";
import {
  CATEGORY_LABELS,
  SEVERITY_LABELS,
  STATUS_LABELS,
  VERDICT_LABELS,
  type IncidentView,
  type VerdictView,
} from "./labels";

const SEVERITY_VARIANT = {
  critical: "destructive",
  high: "destructive",
  medium: "secondary",
  low: "outline",
} as const satisfies Record<IncidentView["severity"], "destructive" | "secondary" | "outline">;

// The severity is always written out; the colour only reinforces it.
export function SeverityBadge({ severity }: { severity: IncidentView["severity"] }) {
  return <Badge variant={SEVERITY_VARIANT[severity]}>{SEVERITY_LABELS[severity]}</Badge>;
}

export function StatusBadge({ status }: { status: IncidentView["status"] }) {
  return <Badge variant="outline">{STATUS_LABELS[status]}</Badge>;
}

export function CategoryText({
  category,
  subtype,
}: {
  category: IncidentView["category"];
  subtype: string | null | undefined;
}) {
  return (
    <span>
      <span>{CATEGORY_LABELS[category]}</span>
      {subtype ? (
        <span className="text-muted-foreground">
          {" / "}
          <span>{subtype}</span>
        </span>
      ) : null}
    </span>
  );
}

export function Age({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} title={formatTime(iso)}>
      {relativeAge(iso)}
    </time>
  );
}

export function VerdictSummary({ verdict }: { verdict: VerdictView }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <Badge variant="secondary">{VERDICT_LABELS[verdict.value]}</Badge>
      {verdict.right_category ? (
        <span className="text-xs text-muted-foreground">
          should be{" "}
          <CategoryText category={verdict.right_category} subtype={verdict.right_subtype} />
        </span>
      ) : null}
    </span>
  );
}

export function IncidentLink({
  projectId,
  incidentId,
  children,
}: {
  projectId: string;
  incidentId: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      to={`/projects/${projectId}/incidents/${incidentId}`}
      className="text-primary underline-offset-2 hover:underline"
    >
      {children}
    </Link>
  );
}
