import type { components } from "@/api/schema";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatTime } from "@/lib/format";
import { deciderLabel, KIND_LABEL, versionChange } from "./labels";

type DecisionRow = components["schemas"]["DecisionRow"];

interface DecisionsTableProps {
  rows: DecisionRow[];
  onOpenReport: (row: DecisionRow & { report_id: string }) => void;
}

export function DecisionsTable({ rows, onOpenReport }: DecisionsTableProps) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>When</TableHead>
          <TableHead>Model</TableHead>
          <TableHead>Kind</TableHead>
          <TableHead>Version</TableHead>
          <TableHead>Decided by</TableHead>
          <TableHead className="w-24">
            <span className="sr-only">Report</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row, index) => (
          // Index keys: the list is read-only and in the server's order, and two decisions can
          // share a second, model and kind.
          <TableRow key={index} data-testid="decision-row">
            <TableCell className="text-xs text-muted-foreground">
              <time dateTime={row.at}>{formatTime(row.at)}</time>
            </TableCell>
            <TableCell className="font-mono text-xs">{row.model}</TableCell>
            <TableCell>
              <Badge variant={row.kind === "promotion" ? "secondary" : "outline"}>
                {KIND_LABEL[row.kind]}
              </Badge>
            </TableCell>
            <TableCell>{versionChange(row.from_version, row.to_version)}</TableCell>
            <TableCell>{deciderLabel(row.decided_by, row.decided_by_ref)}</TableCell>
            <TableCell>
              {row.report_id != null && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    if (row.report_id != null) onOpenReport({ ...row, report_id: row.report_id });
                  }}
                >
                  Report
                </Button>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
