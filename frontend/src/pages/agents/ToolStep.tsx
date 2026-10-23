import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { ToolCallPart, ToolResultPart } from "./session/events";

function toolSummary(name: string, input: Record<string, unknown>): string {
  const lower = name.toLowerCase();
  if (lower === "read") {
    const filePath = input.file_path ?? input.path;
    if (typeof filePath === "string") return filePath;
  }
  if (lower === "grep" || lower === "glob") {
    if (typeof input.pattern === "string") return input.pattern;
  }
  const entries = Object.entries(input);
  if (entries.length === 0) return "";
  return entries
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}

export interface ToolStepProps {
  call: ToolCallPart;
  result?: ToolResultPart;
  className?: string;
}

export function ToolStep({ call, result, className }: ToolStepProps) {
  const [open, setOpen] = useState(false);
  const summary = toolSummary(call.name, call.input);

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className={cn("rounded-md border bg-card p-2 text-sm shadow-xs", className)}
    >
      <CollapsibleTrigger asChild>
        <button
          type="button"
          className="flex w-full items-center justify-between gap-2 text-left hover:opacity-80"
          aria-expanded={open}
        >
          <div className="flex items-center gap-2 overflow-hidden">
            <ChevronRight
              className={cn(
                "size-4 shrink-0 text-muted-foreground transition-transform duration-200",
                open && "rotate-90",
              )}
            />
            <span className="font-medium text-foreground">{call.name}</span>{" "}
            {summary && (
              <span className="truncate font-mono text-xs text-muted-foreground">{summary}</span>
            )}
          </div>
          {!result && (
            <Badge variant="secondary" className="shrink-0 text-[10px]">
              Running
            </Badge>
          )}
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-2 pt-2">
        <div className="rounded bg-muted/50 p-2 font-mono text-xs">
          <div className="mb-1 text-[11px] font-semibold uppercase text-muted-foreground">
            Input
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap">
            <code>{JSON.stringify(call.input, null, 2)}</code>
          </pre>
        </div>
        {result ? (
          <div
            data-error={result.is_error ? "true" : undefined}
            className={cn(
              "rounded bg-muted/50 p-2 font-mono text-xs",
              result.is_error && "border border-destructive/50 bg-destructive/10 text-destructive",
            )}
          >
            <div
              className={cn(
                "mb-1 text-[11px] font-semibold uppercase text-muted-foreground",
                result.is_error && "text-destructive",
              )}
            >
              {result.is_error ? "Error" : "Result"}
            </div>
            <pre className="overflow-x-auto whitespace-pre-wrap">
              <code>{result.content}</code>
            </pre>
          </div>
        ) : (
          <div className="rounded bg-muted/50 p-2 text-xs text-muted-foreground">Running</div>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
