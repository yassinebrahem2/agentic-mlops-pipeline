import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { MessageView } from "./MessageView";
import type { MessageView as MessageViewType } from "./session/events";

export interface ClearedContextEvent {
  person: string;
  last_message_id: string | null;
}

export interface MessageListProps {
  messages: MessageViewType[];
  sessionAgent?: string;
  className?: string;
  clearedEvents?: ClearedContextEvent[];
}

function ClearedDivider({ person }: { person: string }) {
  return (
    <div
      role="separator"
      className="relative flex items-center justify-center py-2 text-xs text-muted-foreground"
    >
      <div className="absolute inset-0 flex items-center">
        <div className="w-full border-t border-border" />
      </div>
      <span className="relative bg-background px-3 font-medium">Context cleared by {person}</span>
    </div>
  );
}

export function MessageList({
  messages,
  sessionAgent,
  className,
  clearedEvents,
}: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);

  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    const threshold = 40;
    const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    isAtBottomRef.current = distanceToBottom <= threshold;
  };

  useEffect(() => {
    if (isAtBottomRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages]);

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className={cn("min-h-0 flex-1 overflow-y-auto p-4 space-y-4", className)}
      role="log"
      aria-label="Messages"
    >
      {clearedEvents
        ?.filter((c) => c.last_message_id === null)
        .map((c, i) => (
          <ClearedDivider key={`cleared-start-${String(i)}`} person={c.person} />
        ))}
      {messages.map((message) => {
        const matchingCleared = clearedEvents?.filter((c) => c.last_message_id === message.id);
        return (
          <div key={message.id} className="space-y-4">
            <MessageView message={message} sessionAgent={sessionAgent} />
            {matchingCleared?.map((c, i) => (
              <ClearedDivider key={`cleared-${message.id}-${String(i)}`} person={c.person} />
            ))}
          </div>
        );
      })}
      <div ref={bottomRef} />
    </div>
  );
}
