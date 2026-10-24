import { useState, type KeyboardEvent } from "react";
import { Send, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { errorText } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface ComposerProps {
  agentName: string;
  agentDisplayName: string;
  busy: boolean;
  onSend: (text: string) => Promise<void>;
  onStop: () => Promise<void>;
  className?: string;
}

export function Composer({ agentDisplayName, busy, onSend, onStop, className }: ComposerProps) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  const canSend = text.trim().length > 0 && !sending;
  const showStop = busy && text.length === 0;

  const handleSend = async () => {
    if (!canSend) return;
    const toSend = text;
    setSending(true);
    setError(null);
    try {
      await onSend(toSend);
      setText("");
    } catch (caught) {
      setError(errorText(caught));
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSend();
    }
  };

  const handleStop = async () => {
    try {
      await onStop();
    } catch (caught) {
      setError(errorText(caught));
    }
  };

  const labelText = `Message ${agentDisplayName}`;

  return (
    <div className={cn("border-t bg-card p-4 space-y-2", className)}>
      <Label htmlFor="composer-textarea" className="sr-only">
        {labelText}
      </Label>
      <div className="flex items-center gap-2">
        <Textarea
          id="composer-textarea"
          aria-label={labelText}
          placeholder={labelText}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            if (error) setError(null);
          }}
          onKeyDown={handleKeyDown}
          rows={1}
          className="min-h-0 h-48 max-h-48 resize-none overflow-y-auto supports-[field-sizing:content]:h-auto"
        />
        <Button
          type="button"
          size="icon"
          variant={showStop ? "outline" : "default"}
          aria-label={showStop ? "Stop" : "Send"}
          disabled={!showStop && !canSend}
          onClick={() => void (showStop ? handleStop() : handleSend())}
        >
          {showStop ? <Square aria-hidden="true" /> : <Send aria-hidden="true" />}
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
