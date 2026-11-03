import { render, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MessageList } from "./MessageList";
import type { MessageView } from "./session/events";

const createMessage = (id: string, text: string): MessageView => ({
  id,
  turn_id: "t1",
  author_kind: "user",
  author_name: "user",
  from_agent: null,
  parts: [{ type: "text", text }],
  status: "complete",
  created_at: "2026-01-01T00:00:00Z",
});

describe("MessageList stick-to-bottom", () => {
  it("scrolls newest content into view when user is at the bottom without smooth scroll", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;

    const { rerender } = render(<MessageList messages={[createMessage("m1", "First")]} />);
    expect(scrollIntoView).toHaveBeenCalled();
    expect(scrollIntoView).not.toHaveBeenCalledWith(
      expect.objectContaining({ behavior: "smooth" }),
    );

    scrollIntoView.mockClear();
    rerender(
      <MessageList messages={[createMessage("m1", "First"), createMessage("m2", "Second")]} />,
    );
    expect(scrollIntoView).toHaveBeenCalled();
    expect(scrollIntoView).not.toHaveBeenCalledWith(
      expect.objectContaining({ behavior: "smooth" }),
    );
  });

  it("resumes following streamed text after the user returns to the bottom", () => {
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    const { getByRole, rerender } = render(
      <MessageList messages={[createMessage("m1", "First")]} />,
    );
    const log = getByRole("log", { name: "Messages" });
    Object.defineProperties(log, {
      scrollHeight: { value: 1000, configurable: true },
      clientHeight: { value: 500, configurable: true },
    });
    log.scrollTop = 100;
    fireEvent.scroll(log);
    scrollIntoView.mockClear();
    rerender(<MessageList messages={[createMessage("m1", "First streamed chunk")]} />);
    expect(scrollIntoView).not.toHaveBeenCalled();

    log.scrollTop = 500;
    fireEvent.scroll(log);
    rerender(<MessageList messages={[createMessage("m1", "First streamed chunk and more")]} />);
    expect(scrollIntoView).toHaveBeenCalled();
    expect(scrollIntoView).not.toHaveBeenCalledWith(
      expect.objectContaining({ behavior: "smooth" }),
    );
    scrollIntoView.mockRestore();
  });

  it("renders at the bottom without behavior 'smooth' when switching sessions even if previously scrolled up", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;

    const { getByRole, rerender } = render(
      <MessageList sessionAgent="agent-1" messages={[createMessage("m1", "Session 1 msg")]} />,
    );
    expect(scrollIntoView).toHaveBeenCalled();
    expect(scrollIntoView).not.toHaveBeenCalledWith(
      expect.objectContaining({ behavior: "smooth" }),
    );

    // User scrolls up in session 1
    const log = getByRole("log", { name: "Messages" });
    Object.defineProperties(log, {
      scrollHeight: { value: 1000, configurable: true },
      clientHeight: { value: 500, configurable: true },
    });
    log.scrollTop = 100;
    fireEvent.scroll(log);

    scrollIntoView.mockClear();

    // Now switch sessions (sessionAgent changes to "agent-2" with session 2 messages)
    rerender(
      <MessageList sessionAgent="agent-2" messages={[createMessage("m2", "Session 2 msg")]} />,
    );

    // It MUST scroll into view at the bottom, without behavior: "smooth"
    expect(scrollIntoView).toHaveBeenCalled();
    expect(scrollIntoView).not.toHaveBeenCalledWith(
      expect.objectContaining({ behavior: "smooth" }),
    );
  });

  it("does not scroll when the user has scrolled up", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;

    const { container, rerender } = render(
      <MessageList messages={[createMessage("m1", "First")]} />,
    );
    scrollIntoView.mockClear();

    const scrollContainer = container.querySelector('[role="log"]') as HTMLElement;
    expect(scrollContainer).toBeInTheDocument();

    // Simulate scrolled up: distance to bottom > 40
    Object.defineProperty(scrollContainer, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scrollContainer, "scrollTop", { value: 100, configurable: true });
    Object.defineProperty(scrollContainer, "clientHeight", { value: 500, configurable: true });
    // 1000 - 100 - 500 = 400 > 40 threshold
    fireEvent.scroll(scrollContainer);

    rerender(
      <MessageList messages={[createMessage("m1", "First"), createMessage("m2", "Second")]} />,
    );
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});

describe("MessageList context cleared divider", () => {
  it("renders 'Context cleared by <person>' divider below the matching message", () => {
    const { getByText } = render(
      <MessageList
        messages={[createMessage("m1", "First"), createMessage("m2", "Second")]}
        clearedEvents={[{ person: "alice", last_message_id: "m1" }]}
      />,
    );
    expect(getByText("Context cleared by alice")).toBeInTheDocument();
  });

  it("renders divider at the start when last_message_id is null", () => {
    const { getByText } = render(
      <MessageList
        messages={[createMessage("m1", "First")]}
        clearedEvents={[{ person: "bob", last_message_id: null }]}
      />,
    );
    expect(getByText("Context cleared by bob")).toBeInTheDocument();
  });
});
