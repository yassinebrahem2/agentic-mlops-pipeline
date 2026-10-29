import { useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { ChevronRight, History, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { api, errorText, unwrap } from "@/lib/api";
import { formatTime } from "@/lib/format";
import type { HandoffView } from "@/lib/useHandoff";
import type { MetricPoint, ModelSummary } from "@/lib/useTracking";
import { trackingKey, useDeployments, useModelVersions } from "@/lib/useTracking";
import { cn } from "@/lib/utils";
import { buildVersionRows, metricNames, primaryMetric } from "./buildVersionRows";
import { VersionsTable } from "./VersionsTable";

export interface ModelCardProps {
  projectId: string;
  model: ModelSummary;
  handoff: HandoffView | null;
}

export function ModelCard({ projectId, model, handoff }: ModelCardProps) {
  const [aliasOpen, setAliasOpen] = useState(false);

  const {
    data: versionsPage,
    isLoading: versionsLoading,
    error: versionsError,
  } = useModelVersions(projectId, model.source_id, model.name);

  const {
    data: deploymentsPage,
    isLoading: deploymentsLoading,
    error: deploymentsError,
  } = useDeployments(projectId, model.source_id, model.name, { limit: 10 });

  const runKeys = useMemo(() => {
    const set = new Set<string>();
    for (const v of versionsPage?.rows ?? []) {
      if (v.run_key) set.add(v.run_key);
    }
    return Array.from(set);
  }, [versionsPage?.rows]);

  const metricQueries = useQueries({
    queries: runKeys.map((runKey) => ({
      queryKey: [...trackingKey(projectId, "metrics"), runKey, undefined] as const,
      queryFn: async () => {
        const res = await api.GET("/api/projects/{project_id}/tracking/metrics", {
          params: {
            path: { project_id: projectId },
            query: { run_key: runKey, latest: true },
          },
        });
        return unwrap(res);
      },
      enabled: Boolean(projectId && runKey),
    })),
  });

  const metricsRecord = useMemo(() => {
    const record: Record<string, MetricPoint[]> = {};
    runKeys.forEach((key, idx) => {
      const data = metricQueries[idx]?.data;
      if (data?.rows) {
        record[key] = data.rows;
      }
    });
    return record;
  }, [runKeys, metricQueries]);

  const versionRows = useMemo(() => {
    return buildVersionRows(versionsPage?.rows ?? [], metricsRecord);
  }, [versionsPage?.rows, metricsRecord]);

  const names = useMemo(() => metricNames(versionRows), [versionRows]);
  const primary = useMemo(() => primaryMetric(handoff, model.name), [handoff, model.name]);

  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-4">
        <div className="flex items-center gap-3">
          <CardTitle className="text-lg font-semibold">{model.name}</CardTitle>
          {model.production_version && (
            <Badge variant="secondary">Production: {model.production_version}</Badge>
          )}
        </div>
        <span className="text-sm text-muted-foreground">
          {model.versions} {model.versions === 1 ? "version" : "versions"}
        </span>
      </CardHeader>

      <CardContent className="space-y-4">
        {versionsError ? (
          <p className="text-sm text-destructive">{errorText(versionsError)}</p>
        ) : versionsLoading ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Loading versions...
          </div>
        ) : (
          <VersionsTable
            projectId={projectId}
            rows={versionRows}
            metricNames={names}
            primaryMetricName={primary}
            truncated={versionsPage?.truncated}
            totalVersions={versionsPage?.total}
          />
        )}

        <div className="pt-2 border-t border-border/50">
          <Collapsible open={aliasOpen} onOpenChange={setAliasOpen}>
            <CollapsibleTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
              >
                <ChevronRight
                  className={cn(
                    "size-3.5 transition-transform duration-200",
                    aliasOpen && "rotate-90",
                  )}
                />
                <History className="size-3.5" />
                <span>Alias history</span>
              </Button>
            </CollapsibleTrigger>

            <CollapsibleContent className="pt-3">
              {deploymentsError ? (
                <p className="text-xs text-destructive">{errorText(deploymentsError)}</p>
              ) : deploymentsLoading ? (
                <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
                  <Loader2 className="size-3.5 animate-spin" />
                  Loading alias history...
                </div>
              ) : !deploymentsPage || deploymentsPage.rows.length === 0 ? (
                <p className="text-xs text-muted-foreground">No alias history recorded</p>
              ) : (
                <div className="space-y-2">
                  <div className="rounded border bg-muted/20">
                    <div className="divide-y divide-border/40">
                      {deploymentsPage.rows.slice(0, 10).map((d) => (
                        <div
                          key={`${d.alias}-${d.seen_at}`}
                          className="flex items-center justify-between px-3 py-1.5 text-xs"
                        >
                          <div className="flex items-center gap-2">
                            <Badge variant="outline" className="text-[10px]">
                              {d.alias}
                            </Badge>
                            <span className="font-mono text-muted-foreground">
                              {d.from_version ?? "–"} → {d.to_version ?? "removed"}
                            </span>
                          </div>
                          <span className="text-muted-foreground">{formatTime(d.seen_at)}</span>
                        </div>
                      ))}
                    </div>
                  </div>

                  {deploymentsPage.truncated && (
                    <p className="text-xs text-muted-foreground">History truncated</p>
                  )}
                </div>
              )}
            </CollapsibleContent>
          </Collapsible>
        </div>
      </CardContent>
    </Card>
  );
}
