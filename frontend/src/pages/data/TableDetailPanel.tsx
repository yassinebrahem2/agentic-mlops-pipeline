import { AlertCircle, AlertTriangle, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { errorText } from "@/lib/api";
import type { TableRef } from "@/lib/useConnections";
import { useTableDetail } from "@/lib/useConnections";
import type { HandoffView } from "@/lib/useHandoff";
import { modelsUsing, roleFor } from "./roles";

export interface TableDetailPanelProps {
  projectId: string;
  connectionId: string;
  connectionName: string;
  table: TableRef;
  handoff: HandoffView | null;
  isExpanded: boolean;
}

export function TableDetailPanel({
  projectId,
  connectionId,
  connectionName,
  table,
  handoff,
  isExpanded,
}: TableDetailPanelProps) {
  const {
    data: detail,
    isLoading,
    error,
  } = useTableDetail(projectId, connectionId, table.name, isExpanded);

  const role = roleFor(handoff, connectionName, table.name);
  const models = modelsUsing(handoff, connectionName, table.name);

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        <span>Loading table details...</span>
      </div>
    );
  }

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertCircle className="size-4" />
        <AlertTitle>Failed to load table details</AlertTitle>
        <AlertDescription>{errorText(error)}</AlertDescription>
      </Alert>
    );
  }

  if (!detail) {
    return null;
  }

  let rowCountDisplay: string;
  if (detail.row_count !== null) {
    rowCountDisplay = detail.row_count.toLocaleString();
  } else if (detail.timed_out) {
    rowCountDisplay = "unknown, timed out";
  } else {
    rowCountDisplay = "–";
  }

  return (
    <div className="space-y-4 pt-2 pb-1 text-sm">
      {detail.incomplete && (
        <Alert variant="warning">
          <AlertTriangle className="size-4" />
          <AlertTitle>Profile incomplete (memory limit)</AlertTitle>
          <AlertDescription>
            DuckDB memory limit was reached during profiling. Data shown below reflects what
            finished.
          </AlertDescription>
        </Alert>
      )}

      {/* Metadata grid */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 rounded-md bg-muted/40 p-3">
        <div>
          <span className="text-xs text-muted-foreground block">Rows</span>
          <span className="font-medium">{rowCountDisplay}</span>
        </div>
        <div>
          <span className="text-xs text-muted-foreground block">Data version</span>
          <span className="font-mono text-xs truncate block">{detail.data_version}</span>
        </div>
        {role && (
          <>
            <div>
              <span className="text-xs text-muted-foreground block">ID column</span>
              <span className="font-mono text-xs">{role.id_column ?? "–"}</span>
            </div>
            <div>
              <span className="text-xs text-muted-foreground block">Time column</span>
              <span className="font-mono text-xs">{role.time_column ?? "–"}</span>
            </div>
          </>
        )}
      </div>

      {/* Models using the table */}
      {models.length > 0 && (
        <div className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">Used by models</span>
          <div className="flex flex-wrap gap-1.5">
            {models.map((m) => (
              <Badge key={`${m.model}:${m.as}`} variant="secondary" className="text-xs font-normal">
                {m.as === "predictions" ? "Predictions" : "Labels"} of {m.model}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {/* Columns table */}
      <div className="space-y-1.5">
        <span className="text-xs font-medium text-muted-foreground">
          Columns ({detail.columns.length})
        </span>
        <div className="rounded-md border max-h-60 overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="bg-muted/50 border-b">
              <tr className="text-left text-muted-foreground">
                <th className="px-3 py-1.5 font-medium">Column</th>
                <th className="px-3 py-1.5 font-medium">Type</th>
              </tr>
            </thead>
            <tbody className="divide-y font-mono">
              {detail.columns.map((col) => (
                <tr key={col.name} className="hover:bg-muted/30">
                  <td className="px-3 py-1 font-medium">{col.name}</td>
                  <td className="px-3 py-1 text-muted-foreground">{col.type}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Summary */}
      {detail.summary && (
        <div className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">Summary</span>
          <pre className="overflow-x-auto whitespace-pre font-mono text-xs p-3 rounded-md bg-muted/50 border">
            {detail.summary}
          </pre>
        </div>
      )}
    </div>
  );
}
