import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { errorText } from "@/lib/api";
import { formatTime } from "@/lib/format";
import { type SilentVerdict, useJudgeSilentAlert, useSilentAlerts } from "./useStats";

const VERDICT_LABELS: Record<SilentVerdict, string> = {
  real_issue: "real issue",
  not_an_issue: "not an issue",
};

export function SilentAlerts({ projectId }: { projectId: string }) {
  const alerts = useSilentAlerts(projectId);
  const judge = useJudgeSilentAlert(projectId);

  return (
    <section aria-labelledby="silent-alerts-title" className="flex flex-col gap-3">
      <div className="space-y-1">
        <h2 id="silent-alerts-title" className="text-lg font-semibold">
          Silent team alerts
        </h2>
        <p className="text-sm text-muted-foreground">
          Alerts from the team's own monitoring that no incident matched. Say whether each was a
          real issue.
        </p>
      </div>

      {judge.isError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Verdict not saved</AlertTitle>
          <AlertDescription>{errorText(judge.error)}</AlertDescription>
        </Alert>
      )}
      {alerts.isPending && (
        <p className="text-sm text-muted-foreground">Loading silent team alerts…</p>
      )}
      {alerts.isError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading silent team alerts</AlertTitle>
          <AlertDescription>{errorText(alerts.error)}</AlertDescription>
        </Alert>
      )}
      {alerts.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">No silent team alerts.</p>
      )}
      {alerts.data && alerts.data.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Check</TableHead>
              <TableHead>Subject</TableHead>
              <TableHead>Time</TableHead>
              <TableHead>Verdict</TableHead>
              <TableHead className="text-right">Judge</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {alerts.data.map((alert) => (
              <TableRow key={alert.result_id}>
                <TableCell className="font-mono">{alert.check_id}</TableCell>
                <TableCell>{alert.subject}</TableCell>
                <TableCell>{formatTime(alert.ran_at)}</TableCell>
                <TableCell>
                  {alert.verdict ? VERDICT_LABELS[alert.verdict] : "not judged"}
                </TableCell>
                <TableCell className="space-x-2 text-right">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={judge.isPending}
                    onClick={() => {
                      judge.mutate({ resultId: alert.result_id, value: "real_issue" });
                    }}
                  >
                    Real issue
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={judge.isPending}
                    onClick={() => {
                      judge.mutate({ resultId: alert.result_id, value: "not_an_issue" });
                    }}
                  >
                    Not an issue
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}
