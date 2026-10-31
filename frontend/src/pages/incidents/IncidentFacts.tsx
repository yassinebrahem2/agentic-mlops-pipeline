import type { ReactNode } from "react";
import { formatTime } from "@/lib/format";
import { Age, IncidentLink } from "./IncidentBadges";
import type { IncidentView } from "./labels";

function Fact({
  label,
  testId,
  children,
}: {
  label: string;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd data-testid={testId} className="text-sm">
        {children}
      </dd>
    </div>
  );
}

function When({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="text-muted-foreground">–</span>;
  return (
    <>
      {formatTime(iso)}{" "}
      <span className="text-xs text-muted-foreground">
        (<Age iso={iso} />)
      </span>
    </>
  );
}

function Numbers({ values }: { values: number[] }) {
  if (values.length === 0) return <span className="text-muted-foreground">–</span>;
  return <>{values.map((n) => `#${String(n)}`).join(", ")}</>;
}

const yesNo = (value: boolean) => (value ? "Yes" : "No");

export function IncidentFacts({
  projectId,
  incident,
}: {
  projectId: string;
  incident: IncidentView;
}) {
  return (
    <dl
      data-testid="incident-facts"
      className="grid grid-cols-1 gap-4 rounded-lg border p-4 sm:grid-cols-2"
    >
      <Fact label="Started">
        <When iso={incident.started_at} />
      </Fact>
      <Fact label="Detected">
        <When iso={incident.detected_at} />
      </Fact>
      <Fact label="Acknowledged">
        <When iso={incident.acknowledged_at} />
      </Fact>
      <Fact label="Mitigated">
        <When iso={incident.mitigated_at} />
      </Fact>
      <Fact label="Resolved">
        <When iso={incident.resolved_at} />
      </Fact>
      <Fact label="Closed">
        <When iso={incident.closed_at} />
      </Fact>
      {incident.snoozed_until && (
        <Fact label="Snoozed until">
          <When iso={incident.snoozed_until} />
        </Fact>
      )}
      <Fact label="Model">{incident.model ?? "–"}</Fact>
      <Fact label="Held" testId="fact-held">
        {yesNo(incident.held)}
      </Fact>
      <Fact label="Resolved offline" testId="fact-resolved-offline">
        {yesNo(incident.resolved_offline)}
      </Fact>
      <Fact label="Reopened" testId="fact-reopened">
        {String(incident.reopened_count)}
      </Fact>
      <Fact label="Merged into">
        {incident.merged_into ? (
          <IncidentLink projectId={projectId} incidentId={incident.merged_into}>
            {incident.merged_into}
          </IncidentLink>
        ) : (
          "–"
        )}
      </Fact>
      <Fact label="Pull requests">
        <Numbers values={incident.prs} />
      </Fact>
      <Fact label="Issues">
        <Numbers values={incident.issues} />
      </Fact>
    </dl>
  );
}
