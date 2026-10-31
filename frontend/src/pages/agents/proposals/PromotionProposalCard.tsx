import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, unwrap } from "@/lib/api";
import type { ApprovalCardComponent } from "../ApprovalCard";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

type MetricValue = components["schemas"]["MetricValue"];
type MetricSet = components["schemas"]["MetricSet"];

function formatMetric(val?: MetricValue | null): string {
  if (!val) return "-";
  const interval =
    val.ci_low != null && val.ci_high != null
      ? ` [${String(val.ci_low)}, ${String(val.ci_high)}]`
      : "";
  return `${String(val.value)}${interval} (n=${String(val.n)})`;
}

interface MetricsComparisonProps {
  champion?: MetricSet | null;
  challenger?: MetricSet | null;
}

function MetricsComparison({ champion, challenger }: MetricsComparisonProps) {
  if (!champion && !challenger) return null;

  const overallKeys = Array.from(
    new Set([...Object.keys(champion?.overall ?? {}), ...Object.keys(challenger?.overall ?? {})]),
  );

  const groupNames = Array.from(
    new Set([
      ...Object.keys(champion?.per_group ?? {}),
      ...Object.keys(challenger?.per_group ?? {}),
    ]),
  );

  return (
    <div className="space-y-3">
      {overallKeys.length > 0 && (
        <div className="space-y-1.5">
          <h5 className="text-xs font-medium text-foreground">Overall Metrics</h5>
          <Table>
            <TableHeader>
              <TableRow className="text-xs">
                <TableHead className="h-7 text-xs">Metric</TableHead>
                <TableHead className="h-7 text-xs">Champion</TableHead>
                <TableHead className="h-7 text-xs">Challenger</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {overallKeys.map((key) => (
                <TableRow key={key} className="text-xs">
                  <TableCell className="py-1 font-mono">{key}</TableCell>
                  <TableCell className="py-1 font-mono text-muted-foreground">
                    {champion ? formatMetric(champion.overall[key]) : "-"}
                  </TableCell>
                  <TableCell className="py-1 font-mono font-medium text-foreground">
                    {challenger ? formatMetric(challenger.overall[key]) : "-"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {groupNames.length > 0 && (
        <div className="space-y-2">
          <h5 className="text-xs font-medium text-foreground">Per Group Metrics</h5>
          {groupNames.map((group) => {
            const groupChamp = champion?.per_group[group] ?? {};
            const groupChal = challenger?.per_group[group] ?? {};
            const metricKeys = Array.from(
              new Set([...Object.keys(groupChamp), ...Object.keys(groupChal)]),
            );

            return (
              <div key={group} className="space-y-1 rounded-md border border-border/60 p-2">
                <span className="text-xs font-semibold text-foreground">{group}</span>
                <Table>
                  <TableHeader>
                    <TableRow className="text-xs">
                      <TableHead className="h-7 text-xs">Metric</TableHead>
                      <TableHead className="h-7 text-xs">Champion</TableHead>
                      <TableHead className="h-7 text-xs">Challenger</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {metricKeys.map((mKey) => (
                      <TableRow key={mKey} className="text-xs">
                        <TableCell className="py-1 font-mono">{mKey}</TableCell>
                        <TableCell className="py-1 font-mono text-muted-foreground">
                          {formatMetric(groupChamp[mKey])}
                        </TableCell>
                        <TableCell className="py-1 font-mono font-medium text-foreground">
                          {formatMetric(groupChal[mKey])}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export const PromotionProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

  const payload = proposal?.kind === "promotion" ? proposal.payload : undefined;
  const reportId = payload?.report_id;

  const { data: report } = useQuery({
    queryKey: ["projects", projectId, "reports", reportId],
    queryFn: async () => {
      if (!reportId) {
        throw new Error("Missing reportId");
      }
      return unwrap(
        await api.GET("/api/projects/{project_id}/reports/{report_id}", {
          params: { path: { project_id: projectId, report_id: reportId } },
        }),
      );
    },
    enabled: Boolean(reportId),
  });

  const evaluation = report?.evaluation;
  const notComputedEntries = Object.entries(evaluation?.not_computed ?? {});

  return (
    <ProposalShell
      approval={approval}
      proposal={proposal}
      isLoading={isLoading}
      isError={isError}
      projectId={projectId}
      toolName={toolName}
      className={className}
    >
      {payload && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <span className="text-muted-foreground">Model:</span>{" "}
              <span className="font-semibold text-foreground">{payload.model}</span>
            </div>
            <div>
              <span className="text-muted-foreground">Candidate Version:</span>{" "}
              <span className="font-semibold text-foreground">{payload.candidate_version}</span>
            </div>
            <div>
              <span className="text-muted-foreground">Champion Version:</span>{" "}
              <span className="font-semibold text-foreground">
                {payload.champion_version ?? "None"}
              </span>
            </div>
            <div>
              <span className="text-muted-foreground">Deploy Hook:</span>{" "}
              <span className="font-mono text-foreground">{payload.deploy_hook_id}</span>
            </div>
          </div>

          {evaluation && (
            <div className="space-y-3 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Evaluation
                </h4>
                <Badge variant="outline" className="text-[10px]">
                  computed by team step
                  {evaluation.payload.computed_where
                    ? ` (${evaluation.payload.computed_where})`
                    : ""}
                </Badge>
              </div>

              <div className="grid grid-cols-2 gap-2 text-xs">
                <div>
                  <span className="text-muted-foreground">Commit:</span>{" "}
                  <span className="font-mono text-foreground">
                    {evaluation.payload.evaluation_commit}
                  </span>
                </div>
                <div>
                  <span className="text-muted-foreground">Gate Config:</span>{" "}
                  <span className="font-mono text-foreground">
                    {evaluation.payload.gate_config_version}
                  </span>
                </div>
              </div>

              {evaluation.payload.decision && (
                <div className="text-xs">
                  <span className="text-muted-foreground">Decision:</span>{" "}
                  <span className="font-semibold capitalize text-foreground">
                    {evaluation.payload.decision}
                  </span>
                  {evaluation.payload.reasons != null && evaluation.payload.reasons.length > 0 && (
                    <span className="text-muted-foreground">
                      {" "}
                      ({evaluation.payload.reasons.join(", ")})
                    </span>
                  )}
                  {evaluation.payload.reasons === null && (
                    <span className="italic text-muted-foreground"> (reasons: not computed)</span>
                  )}
                </div>
              )}

              <MetricsComparison
                champion={evaluation.payload.champion_metrics}
                challenger={evaluation.payload.challenger_metrics}
              />

              {notComputedEntries.length > 0 && (
                <div className="space-y-1 text-xs">
                  <h5 className="font-medium text-muted-foreground">Not Computed:</h5>
                  <ul className="list-disc list-inside space-y-0.5 text-xs">
                    {notComputedEntries.map(([field, prNumber]) => (
                      <li key={field}>
                        <span className="font-mono text-foreground">{field}</span>:{" "}
                        <span className="text-muted-foreground">
                          {prNumber ? `PR #${prNumber}` : "None"}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}

          {report?.draft.assessments && (
            <div className="space-y-2 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
              <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Report Assessments
              </h4>
              <div className="space-y-2 text-xs">
                {Object.entries(report.draft.assessments).map(([name, field]) => (
                  <div key={name} className="space-y-0.5">
                    <span className="font-medium text-foreground">{name}:</span>{" "}
                    {field.value != null ? (
                      <span className="text-muted-foreground">{field.value}</span>
                    ) : (
                      <span className="italic text-muted-foreground">
                        Not applicable: {field.not_applicable_reason}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </ProposalShell>
  );
};
