import type { components } from "@/api/schema";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type EnvironmentRow = components["schemas"]["EnvironmentRow"];

export function EnvironmentsTable({ rows }: { rows: EnvironmentRow[] }) {
  // Grouped here only for display; the rows keep the order the server sent.
  const byModel = new Map<string, EnvironmentRow[]>();
  for (const row of rows) byModel.set(row.model, [...(byModel.get(row.model) ?? []), row]);

  return (
    <div className="space-y-4">
      {[...byModel].map(([model, modelRows]) => (
        <div
          key={model}
          data-testid={`environments-${model}`}
          className="rounded-lg border border-border/80 bg-card p-3 shadow-xs"
        >
          <h3 className="mb-2 font-mono text-sm font-semibold">{model}</h3>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Environment</TableHead>
                <TableHead>Alias</TableHead>
                <TableHead>Version</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {modelRows.map((row) => (
                <TableRow key={`${row.environment}/${row.alias}`}>
                  <TableCell>{row.environment}</TableCell>
                  <TableCell className="font-mono text-xs">{row.alias}</TableCell>
                  <TableCell>
                    {row.version ?? <span className="text-muted-foreground">No version yet</span>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ))}
    </div>
  );
}
