import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { ConnectionView, TableRef } from "@/lib/useConnections";
import type { HandoffView } from "@/lib/useHandoff";
import { cn } from "@/lib/utils";
import { TableDetailPanel } from "./TableDetailPanel";
import { roleFor } from "./roles";

export interface TableRowProps {
  projectId: string;
  connection: ConnectionView;
  table: TableRef;
  handoff: HandoffView | null;
}

export function TableRow({ projectId, connection, table, handoff }: TableRowProps) {
  const [open, setOpen] = useState(false);
  const role = roleFor(handoff, connection.name, table.name);

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="group">
      <div className="flex flex-wrap items-center justify-between gap-3 p-3 hover:bg-muted/30 transition-colors">
        <div className="flex items-center gap-2 min-w-0">
          <CollapsibleTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 gap-1.5 px-2 font-medium hover:bg-muted text-foreground"
              aria-expanded={open}
            >
              <ChevronRight
                className={cn(
                  "size-4 text-muted-foreground transition-transform duration-200",
                  open && "rotate-90",
                )}
              />
              <span className="font-mono text-sm">{table.name}</span>
            </Button>
          </CollapsibleTrigger>
          <Badge variant="outline" className="text-xs font-mono uppercase">
            {table.format}
          </Badge>
          <span className="text-xs text-muted-foreground">
            {table.files.length} {table.files.length === 1 ? "file" : "files"}
          </span>
        </div>

        <div className="flex items-center gap-2">
          {handoff === null ? (
            <span className="text-xs text-muted-foreground">
              Roles appear after you approve the handoff
            </span>
          ) : role ? (
            <Badge variant="secondary" className="text-xs">
              {role.role}
            </Badge>
          ) : (
            <Badge variant="outline" className="text-xs text-muted-foreground">
              Not assigned
            </Badge>
          )}
        </div>
      </div>

      <CollapsibleContent className="px-3 pb-3 border-t bg-muted/10">
        <TableDetailPanel
          projectId={projectId}
          connectionId={connection.id}
          connectionName={connection.name}
          table={table}
          handoff={handoff}
          isExpanded={open}
        />
      </CollapsibleContent>
    </Collapsible>
  );
}
