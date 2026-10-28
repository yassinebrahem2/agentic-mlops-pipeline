import { useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import { useParams } from "react-router";
import remarkGfm from "remark-gfm";
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
import { api, errorText, unwrap } from "@/lib/api";
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

const markdownComponents: Components = {
  table: ({ node: _node, className, ...props }) => (
    <Table className={cn("my-2 border border-border", className)} {...props} />
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
  pre: ({ node: _node, className, ...props }) => (
    <pre className={cn("overflow-x-auto rounded bg-muted/50 p-2", className)} {...props} />
  ),
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
        {message.error_kind === "keys_exhausted" && message.turn_id && sessionAgent && (
          <TryAgain agent={sessionAgent} turnId={message.turn_id} />
        )}
      </div>
    </div>
  );
}

/** Try again on a turn that failed because every key of its provider was exhausted: the turn
 * runs again on its own trigger message (the retry route), never re-sent as the user. */
function TryAgain({ agent, turnId }: { agent: string; turnId: string }) {
  const { projectId } = useParams<{ projectId: string }>();
  const pendingRef = useRef(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A retry is sent to a project's route; without a project there is nothing to try again.
  if (!projectId) return null;

  const handleClick = async () => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    setError(null);
    try {
      const result = await api.POST(
        "/api/projects/{project_id}/agents/{agent}/turns/{turn_id}/retry",
        { params: { path: { project_id: projectId, agent, turn_id: turnId } } },
      );
      unwrap(result);
    } catch (caught) {
      setError(errorText(caught));
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={pending}
        onClick={() => void handleClick()}
      >
        Try again
      </Button>
      {error && (
        <span role="alert" className="text-xs text-destructive">
          {error}
        </span>
      )}
    </div>
  );
}
