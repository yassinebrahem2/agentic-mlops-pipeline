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
  it("scrolls newest content into view when user is at the bottom", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;

    const { rerender } = render(<MessageList messages={[createMessage("m1", "First")]} />);
    expect(scrollIntoView).toHaveBeenCalled();

    scrollIntoView.mockClear();
    rerender(
      <MessageList messages={[createMessage("m1", "First"), createMessage("m2", "Second")]} />,
    );
    expect(scrollIntoView).toHaveBeenCalled();
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
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth" });
    scrollIntoView.mockRestore();
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
