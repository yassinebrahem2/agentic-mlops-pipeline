import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { MessageView as MessageViewType, ToolResultPart } from "./session/events";
import { ToolStep } from "./ToolStep";

export interface MessageViewProps {
  message: MessageViewType;
  className?: string;
}

function dropNode<T extends object>(props: T): Omit<T, "node"> {
  const rest: Record<string, unknown> = { ...props };
  delete rest.node;
  return rest as Omit<T, "node">;
}

const markdownComponents: Components = {
  table: ({ className, ...props }) => (
    <Table className={cn("my-2 border border-border", className)} {...dropNode(props)} />
  ),
  thead: ({ className, ...props }) => <TableHeader className={className} {...dropNode(props)} />,
  tbody: ({ className, ...props }) => <TableBody className={className} {...dropNode(props)} />,
  tr: ({ className, ...props }) => (
    <TableRow className={cn("border-b border-border", className)} {...dropNode(props)} />
  ),
  th: ({ className, ...props }) => (
    <TableHead
      className={cn("border border-border font-semibold", className)}
      {...dropNode(props)}
    />
  ),
  td: ({ className, ...props }) => (
    <TableCell className={cn("border border-border", className)} {...dropNode(props)} />
  ),
  a: ({ className, ...props }) => (
    <a className={cn("text-primary underline", className)} {...dropNode(props)} />
  ),
  pre: ({ className, ...props }) => (
    <pre
      className={cn("overflow-x-auto rounded bg-muted/50 p-2", className)}
      {...dropNode(props)}
    />
  ),
};

export function MessageView({ message, className }: MessageViewProps) {
  if (message.author_kind === "system") {
    return (
      <div
        role="status"
        className={cn(
          "my-2 rounded-md border border-border/60 bg-muted/40 px-4 py-2 text-center text-sm text-muted-foreground",
          className,
        )}
      >
        {message.parts.map((part, idx) =>
          part.type === "text" ? <span key={idx}>{part.text}</span> : null,
        )}
      </div>
    );
  }

  if (message.author_kind === "user") {
    return (
      <div className={cn("flex justify-end", className)}>
        <div className="max-w-[80%] space-y-1">
          <div className="rounded-lg bg-primary px-4 py-2 text-primary-foreground">
            {message.parts.map((part, idx) =>
              part.type === "text" ? (
                <p key={idx} className="whitespace-pre-wrap text-sm">
                  {part.text}
                </p>
              ) : null,
            )}
          </div>
          {message.status === "queued" && (
            <div className="text-right text-xs text-muted-foreground">Queued</div>
          )}
        </div>
      </div>
    );
  }

  // Agent message
  return (
    <div className={cn("flex justify-start", className)}>
      <div className="max-w-[85%] space-y-2">
        {message.parts.map((part, idx) => {
          if (part.type === "tool_call") {
            const result = message.parts.find(
              (p): p is ToolResultPart => p.type === "tool_result" && p.tool_call_id === part.id,
            );
            return <ToolStep key={part.id} call={part} result={result} />;
          }
          if (part.type === "tool_result") {
            return null;
          }
          return (
            <div key={idx} className="text-sm leading-relaxed text-foreground">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {part.text}
              </ReactMarkdown>
            </div>
          );
        })}
        {message.status === "streaming" && (
          <span
            role="status"
            aria-label="Typing..."
            className="inline-flex items-center gap-1 text-muted-foreground"
          >
            <span className="size-1.5 animate-pulse rounded-full bg-current" />
            <span className="size-1.5 animate-pulse rounded-full bg-current [animation-delay:200ms]" />
            <span className="size-1.5 animate-pulse rounded-full bg-current [animation-delay:400ms]" />
          </span>
        )}
        {message.status === "stopped" && (
          <div>
            <Badge variant="outline" className="text-muted-foreground">
              Stopped
            </Badge>
          </div>
        )}
        {message.status === "interrupted" && (
          <div className="text-xs text-destructive">
            Interrupted: the app stopped during this answer
          </div>
        )}
      </div>
    </div>
  );
}
