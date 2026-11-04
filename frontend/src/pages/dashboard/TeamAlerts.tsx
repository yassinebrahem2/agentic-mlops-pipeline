import type { components } from "@/api/schema";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatTime } from "@/lib/format";

type SilentTeamAlert = components["schemas"]["SilentTeamAlert"];

function formatVerdict(verdict: SilentTeamAlert["verdict"]): string {
  if (verdict === "real_issue") return "Real issue";
  if (verdict === "not_an_issue") return "Not an issue";
  return "None";
}

function verdictVariant(
  verdict: SilentTeamAlert["verdict"],
): "destructive" | "secondary" | "outline" {
  if (verdict === "real_issue") return "destructive";
  if (verdict === "not_an_issue") return "secondary";
  return "outline";
}

export function TeamAlerts({ alerts }: { alerts?: SilentTeamAlert[] | null }) {
  if (!alerts || alerts.length === 0) return null;

  const sorted = [...alerts].sort(
    (a, b) => (new Date(b.ran_at).getTime() || 0) - (new Date(a.ran_at).getTime() || 0),
  );

  return (
    <Card aria-labelledby="team-alerts-title">
      <CardHeader>
        <CardTitle id="team-alerts-title" role="heading" aria-level={2}>
          Your team's alerts
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ul aria-label="Your team's alerts" className="space-y-2">
          {sorted.map((alert) => (
            <li
              key={alert.result_id}
              data-testid={`team-alert-${alert.result_id}`}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3 text-sm"
            >
              <div className="min-w-0">
                <p className="font-medium break-words">{alert.check_id}</p>
                <p className="text-xs text-muted-foreground break-words">
                  <span>{alert.subject}</span>
                  <span> · </span>
                  <span>{formatTime(alert.ran_at)}</span>
                </p>
              </div>
              <Badge variant={verdictVariant(alert.verdict)}>{formatVerdict(alert.verdict)}</Badge>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
