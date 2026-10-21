import { describe, expect, it } from "vitest";
import { applyEvent, type SessionState } from "./applyEvent";
import { parseEvent, type MessageView } from "./events";

const message = (
  id: string,
  author_kind: MessageView["author_kind"],
  parts: MessageView["parts"],
  status: MessageView["status"] = "streaming",
): MessageView => ({
  id,
  turn_id: "turn_1",
  author_kind,
  author_name: author_kind,
  from_agent: author_kind === "agent" ? "orchestrator" : null,
  parts,
  status,
  created_at: "2026-01-01T00:00:00Z",
});

describe("parseEvent", () => {
  it("parses the event payloads and rejects malformed data", () => {
    const value = message("m1", "agent", []);
    expect(parseEvent("message.created", JSON.stringify({ message: value }))).toEqual({
      type: "message.created",
      message: value,
    });
    expect(parseEvent("text.delta", JSON.stringify({ message_id: "m1", text: "hello" }))).toEqual({
      type: "text.delta",
      message_id: "m1",
      text: "hello",
    });
    expect(parseEvent("text.delta", "{bad")).toBeNull();
    expect(parseEvent("text.delta", JSON.stringify({ message_id: 1, text: "hello" }))).toBeNull();
    expect(
      parseEvent("turn.ended", JSON.stringify({ turn_id: "turn_1", status: "done", error: null })),
    ).toEqual({ type: "turn.ended", turn_id: "turn_1", status: "done", error: null });
    expect(parseEvent("resync", "{}")).toEqual({ type: "resync" });
  });
});

describe("applyEvent", () => {
  it("folds a streamed message and ignores replayed events", () => {
    let state: SessionState = { messages: [], busy: false, lastSeq: 0 };
    const user = message("u1", "user", [], "queued");
    const answer = message("a1", "agent", []);
    state = applyEvent(state, {
      seq: 1,
      type: "message.created",
      data: { type: "message.created", message: user },
    });
    state = applyEvent(state, {
      seq: 2,
      type: "message.created",
      data: { type: "message.created", message: answer },
    });
    state = applyEvent(state, {
      seq: 3,
      type: "text.delta",
      data: { type: "text.delta", message_id: "a1", text: "hello" },
    });
    state = applyEvent(state, {
      seq: 4,
      type: "tool.started",
      data: {
        type: "tool.started",
        message_id: "a1",
        id: "t1",
        name: "Read",
        input: { path: "README.md" },
      },
    });
    state = applyEvent(state, {
      seq: 5,
      type: "tool.finished",
      data: {
        type: "tool.finished",
        message_id: "a1",
        tool_call_id: "t1",
        content: "file",
        is_error: false,
      },
    });
    state = applyEvent(state, {
      seq: 6,
      type: "text.delta",
      data: { type: "text.delta", message_id: "a1", text: " world" },
    });
    expect(state.messages[1]?.parts).toEqual([
      { type: "text", text: "hello" },
      { type: "tool_call", id: "t1", name: "Read", input: { path: "README.md" } },
      { type: "tool_result", tool_call_id: "t1", content: "file", is_error: false },
      { type: "text", text: " world" },
    ]);
    expect(
      applyEvent(state, {
        seq: 6,
        type: "text.delta",
        data: { type: "text.delta", message_id: "a1", text: "!" },
      }),
    ).toBe(state);
    const complete = message("u1", "user", [], "complete");
    state = applyEvent(state, {
      seq: 7,
      type: "message.updated",
      data: { type: "message.updated", message: complete },
    });
    state = applyEvent(state, {
      seq: 8,
      type: "turn.ended",
      data: { type: "turn.ended", turn_id: "turn_1", status: "done", error: null },
    });
    expect(state.busy).toBe(false);
  });
});
