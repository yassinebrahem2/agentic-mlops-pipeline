import { parseEvent, type MessageView } from "./events";

export type SessionState = { messages: MessageView[]; busy: boolean; lastSeq: number };
export type SequencedEvent = { seq: number; type: string; data: unknown };

function queued(messages: MessageView[]): boolean {
  return messages.some((message) => message.status === "queued");
}

function withMessage(
  messages: MessageView[],
  id: string,
  update: (message: MessageView) => MessageView,
): MessageView[] {
  return messages.map((message) => (message.id === id ? update(message) : message));
}

export function applyEvent(state: SessionState, event: SequencedEvent): SessionState {
  if (event.seq <= state.lastSeq) return state;
  const data = parseEvent(event.type, event.data);
  if (!data) return state;
  let messages = state.messages;
  let busy = state.busy;
  switch (data.type) {
    case "message.created":
      if (!messages.some((message) => message.id === data.message.id)) {
        messages = [...messages, data.message];
      }
      busy = busy || data.message.author_kind === "user";
      break;
    case "text.delta":
      messages = withMessage(messages, data.message_id, (message) => {
        const parts = [...message.parts];
        const last = parts.at(-1);
        if (last?.type === "text")
          parts[parts.length - 1] = { ...last, text: last.text + data.text };
        else parts.push({ type: "text", text: data.text });
        return { ...message, parts, status: "streaming" };
      });
      break;
    case "tool.started":
      messages = withMessage(messages, data.message_id, (message) => ({
        ...message,
        parts: [
          ...message.parts,
          {
            type: "tool_call",
            id: data.tool_call_id,
            name: data.name,
            input: data.input,
          },
        ],
        status: "streaming",
      }));
      break;
    case "tool.finished":
      messages = withMessage(messages, data.message_id, (message) => ({
        ...message,
        parts: [
          ...message.parts,
          {
            type: "tool_result",
            tool_call_id: data.tool_call_id,
            content: data.content,
            is_error: data.is_error,
          },
        ],
      }));
      break;
    case "message.updated":
      messages = messages.some((message) => message.id === data.message.id)
        ? messages.map((message) => (message.id === data.message.id ? data.message : message))
        : [...messages, data.message];
      break;
    case "turn.ended":
      busy = queued(messages);
      break;
    case "resync":
      break;
  }
  return { messages, busy, lastSeq: event.seq };
}
