import { describe, expect, it } from "vitest";
import sessionFold from "../../../../../shared/fixtures/session-fold.json";
import { applyEvent, type SessionState } from "./applyEvent";
import { parseEvent, type MessageView } from "./events";

const createMessage = (
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
  it("parses valid event payloads by SSE event name directly into generated schema types", () => {
    const msg = createMessage("m1", "agent", [{ type: "text", text: "hello" }]);
    expect(parseEvent("message.created", JSON.stringify({ message: msg }))).toEqual({
      type: "message.created",
      message: msg,
    });
    expect(parseEvent("message.updated", JSON.stringify({ message: msg }))).toEqual({
      type: "message.updated",
      message: msg,
    });
    expect(parseEvent("text.delta", JSON.stringify({ message_id: "m1", text: " world" }))).toEqual({
      type: "text.delta",
      message_id: "m1",
      text: " world",
    });
    expect(
      parseEvent(
        "tool.started",
        JSON.stringify({
          message_id: "m1",
          tool_call_id: "t1",
          name: "Read",
          input: { path: "README.md" },
        }),
      ),
    ).toEqual({
      type: "tool.started",
      message_id: "m1",
      tool_call_id: "t1",
      name: "Read",
      input: { path: "README.md" },
    });
    expect(
      parseEvent(
        "tool.finished",
        JSON.stringify({
          message_id: "m1",
          tool_call_id: "t1",
          content: "file content",
          is_error: false,
        }),
      ),
    ).toEqual({
      type: "tool.finished",
      message_id: "m1",
      tool_call_id: "t1",
      content: "file content",
      is_error: false,
    });
    expect(
      parseEvent("turn.ended", JSON.stringify({ turn_id: "turn_1", status: "done", error: null })),
    ).toEqual({
      type: "turn.ended",
      turn_id: "turn_1",
      status: "done",
      error: null,
    });
    expect(parseEvent("resync", "{}")).toEqual({ type: "resync" });
  });

  it("returns null on malformed JSON or unknown event name", () => {
    expect(parseEvent("text.delta", "{not json")).toBeNull();
    expect(parseEvent("text.delta", JSON.stringify("string value"))).toBeNull();
    expect(parseEvent("text.delta", JSON.stringify(123))).toBeNull();
    expect(parseEvent("text.delta", "null")).toBeNull();
    expect(parseEvent("unknown.event", JSON.stringify({}))).toBeNull();
  });
});

describe("applyEvent", () => {
  it("folds session-fold fixture from start through events to intermediate and end states", () => {
    let state: SessionState = {
      messages: sessionFold.start.messages,
      busy: sessionFold.start.busy,
      lastSeq: sessionFold.start.last_seq,
    };

    const events = sessionFold.events;
    for (const event of events) {
      const parsed = parseEvent(event.type, JSON.stringify(event.data));
      expect(parsed).not.toBeNull();
      if (!parsed) continue;

      state = applyEvent(state, { seq: event.seq, ...parsed });
      const seqKey = String(event.seq);
      if (seqKey in sessionFold.after_event) {
        const intermediate =
          sessionFold.after_event[seqKey as keyof typeof sessionFold.after_event];
        expect(state.messages).toEqual(intermediate.messages);
        expect(state.busy).toBe(intermediate.busy);
        expect(state.lastSeq).toBe(intermediate.last_seq);
      }
    }

    expect(state.messages).toEqual(sessionFold.end.messages);
    expect(state.busy).toBe(sessionFold.end.busy);
    expect(state.lastSeq).toBe(sessionFold.end.last_seq);

    const lastEvent = events[events.length - 1];
    expect(lastEvent).toBeDefined();
    if (lastEvent) {
      const parsed = parseEvent(lastEvent.type, JSON.stringify(lastEvent.data));
      expect(parsed).not.toBeNull();
      if (parsed) {
        const replayed = applyEvent(state, { seq: lastEvent.seq, ...parsed });
        expect(replayed).toBe(state);
      }
    }
  });

  it("deltas after a tool result start a new text part", () => {
    let state: SessionState = { messages: [], busy: false, lastSeq: 0 };
    const agentMsg = createMessage("a1", "agent", []);
    state = applyEvent(state, {
      seq: 1,
      type: "message.created",
      message: agentMsg,
    });
    state = applyEvent(state, {
      seq: 2,
      type: "text.delta",
      message_id: "a1",
      text: "Before tool. ",
    });
    state = applyEvent(state, {
      seq: 3,
      type: "tool.started",
      message_id: "a1",
      tool_call_id: "t1",
      name: "Read",
      input: { path: "a.txt" },
    });
    state = applyEvent(state, {
      seq: 4,
      type: "tool.finished",
      message_id: "a1",
      tool_call_id: "t1",
      content: "data",
      is_error: false,
    });
    state = applyEvent(state, {
      seq: 5,
      type: "text.delta",
      message_id: "a1",
      text: "After tool.",
    });

    const msg = state.messages[0];
    expect(msg?.parts).toHaveLength(4);
    expect(msg?.parts).toEqual([
      { type: "text", text: "Before tool. " },
      { type: "tool_call", id: "t1", name: "Read", input: { path: "a.txt" } },
      { type: "tool_result", tool_call_id: "t1", content: "data", is_error: false },
      { type: "text", text: "After tool." },
    ]);
  });

  it("busy follows queued messages", () => {
    let state: SessionState = { messages: [], busy: false, lastSeq: 0 };
    const userMsg = createMessage("u1", "user", [{ type: "text", text: "Question 1" }], "queued");
    state = applyEvent(state, {
      seq: 1,
      type: "message.created",
      message: userMsg,
    });
    expect(state.busy).toBe(true);

    const queuedMsg = createMessage("u2", "user", [{ type: "text", text: "Question 2" }], "queued");
    state = applyEvent(state, {
      seq: 2,
      type: "message.created",
      message: queuedMsg,
    });
    expect(state.busy).toBe(true);

    state = applyEvent(state, {
      seq: 3,
      type: "turn.ended",
      turn_id: "t1",
      status: "done",
      error: null,
    });
    expect(state.busy).toBe(true);

    const completeMsg = { ...userMsg, status: "complete" as const };
    const completeMsg2 = { ...queuedMsg, status: "complete" as const };
    state = applyEvent(state, {
      seq: 4,
      type: "message.updated",
      message: completeMsg,
    });
    state = applyEvent(state, {
      seq: 5,
      type: "message.updated",
      message: completeMsg2,
    });
    state = applyEvent(state, {
      seq: 6,
      type: "turn.ended",
      turn_id: "t1",
      status: "done",
      error: null,
    });
    expect(state.busy).toBe(false);
  });
});
