import type { components } from "@/api/schema";

type Schemas = components["schemas"];

export type MessageView = Schemas["MessageView"];
export type SessionView = Schemas["SessionView"];
export type TextPart = Schemas["TextPart"];
export type ToolCallPart = Schemas["ToolCallPart"];
export type ToolResultPart = Schemas["ToolResultPart"];
export type Part = Schemas["MessageView"]["parts"][number];
export type MessageCreated = Schemas["MessageCreated"];
export type MessageUpdated = Schemas["MessageUpdated"];
export type TextDelta = Schemas["TextDelta"];
export type ToolStarted = Schemas["ToolStarted"];
export type ToolFinished = Schemas["ToolFinished"];
export type TurnEnded = Schemas["TurnEnded"];
export type Resync = Schemas["Resync"];

export type StreamEventData =
  | { type: "message.created"; message: MessageView }
  | { type: "text.delta"; message_id: string; text: string }
  | {
      type: "tool.started";
      message_id: string;
      id: string;
      name: string;
      input: Record<string, unknown>;
    }
  | {
      type: "tool.finished";
      message_id: string;
      tool_call_id: string;
      content: string;
      is_error: boolean;
    }
  | { type: "message.updated"; message: MessageView }
  | {
      type: "turn.ended";
      turn_id: string;
      status: "done" | "stopped" | "interrupted" | "failed";
      error: string | null;
    }
  | { type: "resync" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function isMessage(value: unknown): value is MessageView {
  if (!isRecord(value) || typeof value.id !== "string" || !Array.isArray(value.parts)) return false;
  if (
    !value.parts.every((part) => {
      if (!isRecord(part) || typeof part.type !== "string") return false;
      if (part.type === "text") return typeof part.text === "string";
      if (part.type === "tool_call") {
        return typeof part.id === "string" && typeof part.name === "string" && isRecord(part.input);
      }
      return (
        part.type === "tool_result" &&
        typeof part.tool_call_id === "string" &&
        typeof part.content === "string" &&
        typeof part.is_error === "boolean"
      );
    })
  ) {
    return false;
  }
  return (
    (value.author_kind === "user" ||
      value.author_kind === "agent" ||
      value.author_kind === "system") &&
    typeof value.author_name === "string" &&
    (typeof value.turn_id === "string" || value.turn_id === null) &&
    (typeof value.from_agent === "string" || value.from_agent === null) &&
    ["complete", "streaming", "queued", "stopped", "interrupted"].includes(String(value.status)) &&
    typeof value.created_at === "string"
  );
}

export function parseEvent(type: string, data: string): StreamEventData | null {
  let value: unknown;
  try {
    value = JSON.parse(data) as unknown;
  } catch {
    return null;
  }
  if (!isRecord(value)) return null;
  switch (type) {
    case "message.created":
      return isMessage(value.message) ? { type, message: value.message } : null;
    case "message.updated":
      return isMessage(value.message) ? { type, message: value.message } : null;
    case "text.delta":
      return typeof value.message_id === "string" && typeof value.text === "string"
        ? { type, message_id: value.message_id, text: value.text }
        : null;
    case "tool.started":
      return typeof value.message_id === "string" &&
        typeof value.name === "string" &&
        isRecord(value.input) &&
        (typeof value.id === "string" || typeof value.tool_call_id === "string")
        ? {
            type,
            message_id: value.message_id,
            id: typeof value.id === "string" ? value.id : String(value.tool_call_id),
            name: value.name,
            input: value.input,
          }
        : null;
    case "tool.finished":
      return typeof value.message_id === "string" &&
        typeof value.tool_call_id === "string" &&
        typeof value.content === "string" &&
        typeof value.is_error === "boolean"
        ? {
            type,
            message_id: value.message_id,
            tool_call_id: value.tool_call_id,
            content: value.content,
            is_error: value.is_error,
          }
        : null;
    case "turn.ended":
      return typeof value.turn_id === "string" &&
        (value.status === "done" ||
          value.status === "stopped" ||
          value.status === "interrupted" ||
          value.status === "failed") &&
        (typeof value.error === "string" || value.error === null)
        ? { type, turn_id: value.turn_id, status: value.status, error: value.error }
        : null;
    case "resync":
      return { type };
    default:
      return null;
  }
}
