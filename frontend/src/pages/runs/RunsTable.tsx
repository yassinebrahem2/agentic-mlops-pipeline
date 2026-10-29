import { ExternalLink, FlaskConical, Workflow } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDuration, formatTime } from "@/lib/format";
import type { RunListItem } from "./mergeRuns";
import { StatusBadge } from "./StatusBadge";

export function RunsTable({ items }: { items: RunListItem[] }) {
  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-12">Kind</TableHead>
            <TableHead>Name</TableHead>
            <TableHead className="w-28">Status</TableHead>
            <TableHead className="w-44">Started</TableHead>
            <TableHead className="w-32">Duration</TableHead>
            <TableHead>Detail</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item) => (
            <TableRow key={`${item.kind}-${item.key}`}>
              <TableCell>
                {item.kind === "run" ? (
                  <span title="Run" className="inline-flex items-center">
                    <FlaskConical className="size-4 text-muted-foreground" aria-hidden="true" />
                    <span className="sr-only">Run</span>
                  </span>
                ) : (
                  <span title="CI" className="inline-flex items-center">
                    <Workflow className="size-4 text-muted-foreground" aria-hidden="true" />
                    <span className="sr-only">CI</span>
                  </span>
                )}
              </TableCell>
              <TableCell className="font-medium">{item.name}</TableCell>
              <TableCell>
                <StatusBadge status={item.status} />
              </TableCell>
              <TableCell className="text-muted-foreground">{formatTime(item.start)}</TableCell>
              <TableCell className="text-muted-foreground">
                {formatDuration(item.durationSeconds)}
              </TableCell>
              <TableCell>
                {item.kind === "ci" && item.url ? (
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 font-mono text-xs text-primary underline-offset-4 hover:underline"
                  >
                    <span>{item.detail ?? item.url}</span>
                    <ExternalLink className="size-3" aria-hidden="true" />
                  </a>
                ) : (
                  <span className="text-muted-foreground text-xs">{item.detail ?? "–"}</span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
