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

export type MessageData = Schemas["MessageData"];
export type TextDeltaData = Schemas["TextDeltaData"];
export type ToolStartedData = Schemas["ToolStartedData"];
export type ToolFinishedData = Schemas["ToolFinishedData"];
export type TurnEndedData = Schemas["TurnEndedData"];
export type ResyncData = Schemas["ResyncData"];

export type StreamEventData =
  | ({ type: "message.created" } & MessageData)
  | ({ type: "text.delta" } & TextDeltaData)
  | ({ type: "tool.started" } & ToolStartedData)
  | ({ type: "tool.finished" } & ToolFinishedData)
  | ({ type: "message.updated" } & MessageData)
  | ({ type: "turn.ended" } & TurnEndedData)
  | { type: "resync" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

export function isMessageData(value: unknown): value is MessageData {
  return isRecord(value) && isRecord(value.message) && typeof value.message.id === "string";
}

export function isTextDeltaData(value: unknown): value is TextDeltaData {
  return isRecord(value) && typeof value.message_id === "string" && typeof value.text === "string";
}

export function isToolStartedData(value: unknown): value is ToolStartedData {
  return (
    isRecord(value) &&
    typeof value.message_id === "string" &&
    typeof value.tool_call_id === "string" &&
    typeof value.name === "string" &&
    isRecord(value.input)
  );
}

export function isToolFinishedData(value: unknown): value is ToolFinishedData {
  return (
    isRecord(value) &&
    typeof value.message_id === "string" &&
    typeof value.tool_call_id === "string" &&
    typeof value.content === "string" &&
    typeof value.is_error === "boolean"
  );
}

export function isTurnEndedData(value: unknown): value is TurnEndedData {
  return (
    isRecord(value) &&
    typeof value.turn_id === "string" &&
    (value.status === "done" ||
      value.status === "stopped" ||
      value.status === "interrupted" ||
      value.status === "failed") &&
    (typeof value.error === "string" || value.error === null)
  );
}

export function isResyncData(value: unknown): value is ResyncData {
  return isRecord(value);
}

export function parseEvent(type: string, data: unknown): StreamEventData | null {
  let value: unknown = data;
  if (typeof data === "string") {
    try {
      value = JSON.parse(data) as unknown;
    } catch {
      return null;
    }
  }
  switch (type) {
    case "message.created":
      return isMessageData(value) ? { type, ...value } : null;
    case "message.updated":
      return isMessageData(value) ? { type, ...value } : null;
    case "text.delta":
      return isTextDeltaData(value) ? { type, ...value } : null;
    case "tool.started":
      return isToolStartedData(value) ? { type, ...value } : null;
    case "tool.finished":
      return isToolFinishedData(value) ? { type, ...value } : null;
    case "turn.ended":
      return isTurnEndedData(value) ? { type, ...value } : null;
    case "resync":
      return isResyncData(value) ? { type } : null;
    default:
      return null;
  }
}
