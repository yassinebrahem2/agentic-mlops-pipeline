import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { MessageView } from "./MessageView";
import type { MessageView as MessageViewType } from "./session/events";

export interface MessageListProps {
  messages: MessageViewType[];
  className?: string;
}

export function MessageList({ messages, className }: MessageListProps) {
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
      className={cn("flex-1 overflow-y-auto p-4 space-y-4", className)}
      role="log"
      aria-label="Messages"
    >
      {messages.map((message) => (
        <MessageView key={message.id} message={message} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
