import type { MessageView, StreamEventData } from "./events";
import type { TaskView } from "../TaskBar";

export type SessionState = {
  messages: MessageView[];
  busy: boolean;
  lastSeq: number;
  current_task?: TaskView | null;
};
export type SequencedEvent = { seq: number } & StreamEventData;

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
  let messages = state.messages;
  let busy = state.busy;
  switch (event.type) {
    case "message.created":
      if (!messages.some((message) => message.id === event.message.id)) {
        messages = [...messages, event.message];
      }
      busy = busy || event.message.author_kind === "user";
      break;
    case "text.delta":
      messages = withMessage(messages, event.message_id, (message) => {
        const parts = [...message.parts];
        const last = parts.at(-1);
        if (last?.type === "text")
          parts[parts.length - 1] = { ...last, text: last.text + event.text };
        else parts.push({ type: "text", text: event.text });
        return { ...message, parts, status: "streaming" };
      });
      break;
    case "tool.started":
      messages = withMessage(messages, event.message_id, (message) => ({
        ...message,
        parts: [
          ...message.parts,
          {
            type: "tool_call",
            id: event.tool_call_id,
            name: event.name,
            input: event.input,
            extra_content: null,
            model: null,
          },
        ],
        status: "streaming",
      }));
      break;
    case "tool.finished":
      messages = withMessage(messages, event.message_id, (message) => ({
        ...message,
        parts: [
          ...message.parts,
          {
            type: "tool_result",
            tool_call_id: event.tool_call_id,
            content: event.content,
            is_error: event.is_error,
          },
        ],
      }));
      break;
    case "message.updated":
      messages = messages.some((message) => message.id === event.message.id)
        ? messages.map((message) => (message.id === event.message.id ? event.message : message))
        : [...messages, event.message];
      break;
    case "turn.ended":
      busy = queued(messages);
      break;
    case "turn.waiting":
      messages = messages.map((message) =>
        message.turn_id === event.turn_id &&
        message.author_kind === "agent" &&
        message.status === "streaming"
          ? { ...message, status: "waiting" }
          : message,
      );
      break;
    case "resync":
      break;
  }
  return { messages, busy, lastSeq: event.seq, current_task: state.current_task };
}
