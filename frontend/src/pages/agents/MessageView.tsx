import { createContext, useContext } from "react";
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
import { ApprovalCard } from "./ApprovalCard";
import { ToolStep } from "./ToolStep";
import type {
  MessageView as MessageViewType,
  ToolCallPart,
  ToolResultPart,
} from "./session/events";

export interface MessageViewProps {
  message: MessageViewType;
  sessionAgent?: string;
  className?: string;
}

const PreContext = createContext(false);

function MarkdownPre({
  node: _node,
  className,
  children,
  ...props
}: React.ComponentProps<"pre"> & { node?: unknown }) {
  return (
    <pre className={cn("my-3 overflow-x-auto rounded-lg bg-muted/50 p-3", className)} {...props}>
      <PreContext.Provider value={true}>{children}</PreContext.Provider>
    </pre>
  );
}

function MarkdownCode({
  node: _node,
  className,
  ...props
}: React.ComponentProps<"code"> & { node?: unknown }) {
  const isInsidePre = useContext(PreContext);
  if (isInsidePre) {
    return <code className={className} {...props} />;
  }
  return (
    <code
      className={cn("rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-foreground", className)}
      {...props}
    />
  );
}

const markdownComponents: Components = {
  h1: ({ node: _node, className, ...props }) => (
    <h1
      className={cn("mt-6 mb-3 text-2xl font-bold text-foreground first:mt-0", className)}
      {...props}
    />
  ),
  h2: ({ node: _node, className, ...props }) => (
    <h2
      className={cn("mt-5 mb-2.5 text-xl font-semibold text-foreground first:mt-0", className)}
      {...props}
    />
  ),
  h3: ({ node: _node, className, ...props }) => (
    <h3
      className={cn("mt-4 mb-2 text-lg font-semibold text-foreground first:mt-0", className)}
      {...props}
    />
  ),
  h4: ({ node: _node, className, ...props }) => (
    <h4
      className={cn("mt-3 mb-1.5 text-base font-semibold text-foreground first:mt-0", className)}
      {...props}
    />
  ),
  h5: ({ node: _node, className, ...props }) => (
    <h5
      className={cn("mt-2.5 mb-1 text-sm font-semibold text-foreground first:mt-0", className)}
      {...props}
    />
  ),
  h6: ({ node: _node, className, ...props }) => (
    <h6
      className={cn("mt-2 mb-1 text-xs font-semibold text-muted-foreground first:mt-0", className)}
      {...props}
    />
  ),
  p: ({ node: _node, className, ...props }) => (
    <p className={cn("my-3.5 leading-relaxed first:mt-0 last:mb-0", className)} {...props} />
  ),
  ul: ({ node: _node, className, ...props }) => (
    <ul
      className={cn("my-3 list-disc pl-6 space-y-1.5 first:mt-0 last:mb-0", className)}
      {...props}
    />
  ),
  ol: ({ node: _node, className, ...props }) => (
    <ol
      className={cn("my-3 list-decimal pl-6 space-y-1.5 first:mt-0 last:mb-0", className)}
      {...props}
    />
  ),
  li: ({ node: _node, className, ...props }) => (
    <li className={cn("leading-relaxed", className)} {...props} />
  ),
  blockquote: ({ node: _node, className, ...props }) => (
    <blockquote
      className={cn(
        "my-3.5 border-l-2 border-border pl-4 italic text-muted-foreground first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  hr: ({ node: _node, className, ...props }) => (
    <hr className={cn("my-4 border-t border-border", className)} {...props} />
  ),
  table: ({ node: _node, className, ...props }) => (
    <Table className={cn("my-4 border border-border", className)} {...props} />
  ),
  thead: ({ node: _node, className, ...props }) => <TableHeader className={className} {...props} />,
  tbody: ({ node: _node, className, ...props }) => <TableBody className={className} {...props} />,
  tr: ({ node: _node, className, ...props }) => (
    <TableRow className={cn("border-b border-border", className)} {...props} />
  ),
  th: ({ node: _node, className, ...props }) => (
    <TableHead className={cn("border border-border font-semibold", className)} {...props} />
  ),
  td: ({ node: _node, className, ...props }) => (
    <TableCell className={cn("border border-border", className)} {...props} />
  ),
  a: ({ node: _node, className, ...props }) => (
    <a className={cn("text-primary underline", className)} {...props} />
  ),
  pre: MarkdownPre,
  code: MarkdownCode,
};

export function MessageView({ message, sessionAgent, className }: MessageViewProps) {
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
    const isFromAgent = Boolean(message.from_agent);
    const label = isFromAgent
      ? message.from_agent === sessionAgent
        ? message.author_name
        : `From ${message.author_name}`
      : null;

    return (
      <div className={cn("flex justify-end", className)}>
        <div className="max-w-[80%] space-y-1">
          {label && (
            <div className="text-right text-xs font-medium text-muted-foreground">{label}</div>
          )}
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
          if (part.type === "approval") {
            const matchingTool = message.parts.find(
              (p): p is ToolCallPart => p.type === "tool_call" && p.id === part.tool_call_id,
            );
            return (
              <div key={part.approval_id} className="space-y-2">
                <ApprovalCard toolName={matchingTool?.name} approval={part} />
              </div>
            );
          }
          return (
            <div key={idx} className="text-base leading-relaxed text-foreground">
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
        {message.status === "waiting" && (
          <div role="status" className="text-xs text-muted-foreground">
            Waiting for your approval
          </div>
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
