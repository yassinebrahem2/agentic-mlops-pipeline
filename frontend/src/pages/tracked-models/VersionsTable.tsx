import { useState } from "react";
import { Link } from "react-router";
import { ChevronRight, ExternalLink, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatTime, shortCommit } from "@/lib/format";
import type { RunRow } from "@/lib/useTracking";
import { useRunByKey } from "@/lib/useTracking";
import { cn } from "@/lib/utils";
import { StatusBadge } from "@/pages/runs/StatusBadge";
import { formatMetric, type VersionRow } from "./buildVersionRows";

export interface VersionsTableProps {
  projectId: string;
  rows: VersionRow[];
  metricNames: string[];
  primaryMetricName: string | null;
  truncated?: boolean;
  totalVersions?: number;
}

export function VersionsTable({
  projectId,
  rows,
  metricNames,
  primaryMetricName,
  truncated,
  totalVersions,
}: VersionsTableProps) {
  const columnCount = 3 + metricNames.length;

  return (
    <div className="space-y-2">
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-24">Version</TableHead>
              <TableHead className="w-40">Aliases</TableHead>
              <TableHead className="w-44">Created</TableHead>
              {metricNames.map((name) => (
                <TableHead key={name}>
                  <div className="flex items-center gap-1.5">
                    <span>{name}</span>
                    {primaryMetricName === name && (
                      <Badge variant="outline" className="text-[10px] uppercase tracking-wide">
                        primary
                      </Badge>
                    )}
                  </div>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={columnCount}
                  className="py-6 text-center text-sm text-muted-foreground"
                >
                  No versions recorded
                </TableCell>
              </TableRow>
            ) : (
              rows.map((row) => (
                <VersionRowItem
                  key={row.version}
                  projectId={projectId}
                  row={row}
                  metricNames={metricNames}
                  columnCount={columnCount}
                />
              ))
            )}
          </TableBody>
        </Table>
      </div>

      {truncated && totalVersions !== undefined && (
        <p className="text-sm text-muted-foreground">
          Showing the latest {String(rows.length)} of {String(totalVersions)} versions
        </p>
      )}
    </div>
  );
}

function VersionRowItem({
  projectId,
  row,
  metricNames,
  columnCount,
}: {
  projectId: string;
  row: VersionRow;
  metricNames: string[];
  columnCount: number;
}) {
  const [open, setOpen] = useState(false);
  const { data: run, isLoading: runLoading } = useRunByKey(projectId, row.runKey, open);

  return (
    <>
      <TableRow className="h-10 hover:bg-muted/50">
        <TableCell>
          <button
            type="button"
            onClick={() => {
              setOpen((prev) => !prev);
            }}
            className="flex items-center gap-1.5 font-medium hover:underline text-left text-foreground"
            aria-expanded={open}
          >
            <ChevronRight
              className={cn(
                "size-4 text-muted-foreground transition-transform duration-200",
                open && "rotate-90",
              )}
            />
            <span>{row.version}</span>
          </button>
        </TableCell>
        <TableCell>
          <div className="flex flex-wrap gap-1">
            {row.aliases.length > 0 ? (
              row.aliases.map((alias) => (
                <Badge key={alias} variant="secondary" className="text-xs">
                  {alias}
                </Badge>
              ))
            ) : (
              <span className="text-xs text-muted-foreground">–</span>
            )}
          </div>
        </TableCell>
        <TableCell className="text-sm text-muted-foreground">{formatTime(row.created)}</TableCell>
        {metricNames.map((name) => (
          <TableCell key={name} className="font-mono text-sm">
            {row.metrics[name] !== undefined ? formatMetric(row.metrics[name]) : "–"}
          </TableCell>
        ))}
      </TableRow>

      {open && (
        <TableRow className="bg-muted/30 hover:bg-muted/30">
          <TableCell colSpan={columnCount} className="p-4">
            <Collapsible open={open} onOpenChange={setOpen}>
              <CollapsibleContent forceMount className="space-y-3">
                <VersionLineage
                  projectId={projectId}
                  runKey={row.runKey}
                  run={run}
                  runLoading={runLoading}
                />
              </CollapsibleContent>
            </Collapsible>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function VersionLineage({
  projectId,
  runKey,
  run,
  runLoading,
}: {
  projectId: string;
  runKey: string | null;
  run: RunRow | null | undefined;
  runLoading: boolean;
}) {
  if (!runKey) {
    return <p className="text-sm text-muted-foreground">Run not tracked</p>;
  }

  if (runLoading) {
    return (
      <div className="flex items-center gap-2 py-1 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        Loading run...
      </div>
    );
  }

  if (!run) {
    return <p className="text-sm text-muted-foreground">Run not tracked</p>;
  }

  const shortSha = shortCommit(run.git_commit);
  const datasetsText = run.datasets.length > 0 ? run.datasets.join(", ") : "Not recorded";
  const paramsEntries = Object.entries(run.params);
  const tagsEntries = Object.entries(run.tags);

  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-medium text-foreground">{run.experiment}</span>
        <StatusBadge status={run.status} />
        <span className="text-muted-foreground">Started {formatTime(run.start)}</span>
        <Link
          to={`/projects/${projectId}/runs`}
          className="inline-flex items-center gap-1 text-xs text-primary underline-offset-4 hover:underline"
        >
          <span>Runs page</span>
          <ExternalLink className="size-3" aria-hidden="true" />
        </Link>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div className="flex items-center gap-2 text-xs">
          <span className="font-medium text-muted-foreground">Git commit:</span>
          {shortSha ? (
            <span
              title={run.git_commit ?? undefined}
              className="font-mono rounded bg-muted px-1.5 py-0.5 text-foreground"
            >
              {shortSha}
            </span>
          ) : (
            <span className="text-muted-foreground">–</span>
          )}
        </div>

        <div className="flex items-center gap-2 text-xs">
          <span className="font-medium text-muted-foreground">Training data:</span>
          <span className="text-foreground">{datasetsText}</span>
        </div>
      </div>

      {(paramsEntries.length > 0 || tagsEntries.length > 0) && (
        <div className="space-y-2 pt-1 border-t border-border/40 text-xs">
          {paramsEntries.length > 0 && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="font-medium text-muted-foreground">Params:</span>
              {paramsEntries.map(([k, v]) => (
                <span key={k} className="font-mono text-muted-foreground">
                  <span className="text-foreground">{k}</span>={v}
                </span>
              ))}
            </div>
          )}

          {tagsEntries.length > 0 && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="font-medium text-muted-foreground">Tags:</span>
              {tagsEntries.map(([k, v]) => (
                <span key={k} className="font-mono text-muted-foreground">
                  <span className="text-foreground">{k}</span>={v}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
