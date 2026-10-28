import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { MessageView } from "./MessageView";
import type { MessageView as MessageViewType } from "./session/events";

export interface MessageListProps {
  messages: MessageViewType[];
  sessionAgent?: string;
  /** The session's send call; Try again on a failed turn re-sends its trigger through it. */
  onSend?: (text: string) => Promise<void>;
  className?: string;
}

/** The text of the user message that started the turn, when it is in the list. */
function triggerText(messages: MessageViewType[], turnId: string | null): string | null {
  if (turnId === null) return null;
  const trigger = messages.find(
    (message) => message.author_kind === "user" && message.turn_id === turnId,
  );
  if (!trigger) return null;
  return trigger.parts.map((part) => (part.type === "text" ? part.text : "")).join("");
}

export function MessageList({ messages, sessionAgent, onSend, className }: MessageListProps) {
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
      {messages.map((message) => {
        const text =
          message.error_kind === "keys_exhausted" ? triggerText(messages, message.turn_id) : null;
        return (
          <MessageView
            key={message.id}
            message={message}
            sessionAgent={sessionAgent}
            onTryAgain={onSend && text ? () => onSend(text) : undefined}
          />
        );
      })}
      <div ref={bottomRef} />
    </div>
  );
}
