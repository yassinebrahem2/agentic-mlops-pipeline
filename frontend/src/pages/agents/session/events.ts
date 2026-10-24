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
export type TurnWaiting = Schemas["TurnWaiting"];
export type Resync = Schemas["Resync"];

export type MessageData = Schemas["MessageData"];
export type TextDeltaData = Schemas["TextDeltaData"];
export type ToolStartedData = Schemas["ToolStartedData"];
export type ToolFinishedData = Schemas["ToolFinishedData"];
export type TurnEndedData = Schemas["TurnEndedData"];
export type TurnWaitingData = Schemas["TurnWaitingData"];

export const eventNames = [
  "message.created",
  "text.delta",
  "tool.started",
  "tool.finished",
  "message.updated",
  "turn.ended",
  "turn.waiting",
  "resync",
] as const;

export type StreamEventName = (typeof eventNames)[number];

export type StreamEventData =
  | ({ type: "message.created" } & MessageData)
  | ({ type: "text.delta" } & TextDeltaData)
  | ({ type: "tool.started" } & ToolStartedData)
  | ({ type: "tool.finished" } & ToolFinishedData)
  | ({ type: "message.updated" } & MessageData)
  | ({ type: "turn.ended" } & TurnEndedData)
  | ({ type: "turn.waiting" } & TurnWaitingData)
  | { type: "resync" };

export function parseEvent(type: string, data: string): StreamEventData | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data) as unknown;
  } catch {
    return null;
  }

  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }

  switch (type) {
    case "message.created":
      return { type, ...(parsed as MessageData) };
    case "message.updated":
      return { type, ...(parsed as MessageData) };
    case "text.delta":
      return { type, ...(parsed as TextDeltaData) };
    case "tool.started":
      return { type, ...(parsed as ToolStartedData) };
    case "tool.finished":
      return { type, ...(parsed as ToolFinishedData) };
    case "turn.ended":
      return { type, ...(parsed as TurnEndedData) };
    case "turn.waiting":
      return { type, ...(parsed as TurnWaitingData) };
    case "resync":
      return { type };
    default:
      return null;
  }
}
