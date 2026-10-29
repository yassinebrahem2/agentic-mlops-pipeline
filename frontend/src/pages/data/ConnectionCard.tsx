import { AlertCircle, Database, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConnectionFindings } from "@/components/ConnectionFindings";
import { errorText } from "@/lib/api";
import type { ConnectionView } from "@/lib/useConnections";
import { useTables } from "@/lib/useConnections";
import type { HandoffView } from "@/lib/useHandoff";
import { TableRow } from "./TableRow";

export interface ConnectionCardProps {
  projectId: string;
  connection: ConnectionView;
  handoff: HandoffView | null;
}

export function ConnectionCard({ projectId, connection, handoff }: ConnectionCardProps) {
  const isUnreachable = connection.status === "unreachable";

  // Tables are NOT requested when connection is unreachable
  const {
    data: tables,
    isLoading: tablesLoading,
    error: tablesError,
  } = useTables(projectId, isUnreachable ? undefined : connection.id);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-4">
        <div className="flex items-center gap-3">
          <Database className="size-5 text-muted-foreground" />
          <div>
            <CardTitle className="text-base font-semibold">{connection.name}</CardTitle>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="font-mono text-xs">
            {connection.kind}
          </Badge>
          <Badge
            variant={isUnreachable ? "destructive" : "secondary"}
            className="text-xs font-medium"
          >
            {connection.status}
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-4 pt-0">
        {/* Unreachable detail */}
        {isUnreachable && connection.status_detail && (
          <Alert variant="destructive">
            <AlertCircle className="size-4" />
            <AlertTitle>Connection unreachable</AlertTitle>
            <AlertDescription>{connection.status_detail}</AlertDescription>
          </Alert>
        )}

        {/* Findings: rendered only via ConnectionFindings */}
        <ConnectionFindings findings={connection.findings} />

        {/* Tables section when reachable */}
        {!isUnreachable && (
          <div className="space-y-2">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Tables
            </h4>

            {tablesLoading && (
              <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                <span>Loading tables...</span>
              </div>
            )}

            {tablesError && (
              <Alert variant="destructive">
                <AlertCircle className="size-4" />
                <AlertTitle>Error loading tables</AlertTitle>
                <AlertDescription>{errorText(tablesError)}</AlertDescription>
              </Alert>
            )}

            {!tablesLoading && !tablesError && (!tables || tables.length === 0) && (
              <p className="text-sm text-muted-foreground italic">No tables found</p>
            )}

            {!tablesLoading && !tablesError && tables && tables.length > 0 && (
              <div className="divide-y rounded-md border">
                {tables.map((table) => (
                  <TableRow
                    key={table.name}
                    projectId={projectId}
                    connection={connection}
                    table={table}
                    handoff={handoff}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
