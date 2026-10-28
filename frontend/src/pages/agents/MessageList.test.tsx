import { render, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { MessageList } from "./MessageList";
import type { MessageView } from "./session/events";

const trigger: MessageView = {
  id: "u1",
  turn_id: "trn_1",
  author_kind: "user",
  author_name: "user",
  from_agent: null,
  parts: [{ type: "text", text: "Train the baseline model" }],
  status: "complete",
  created_at: "2026-01-01T00:00:00Z",
};

const failedAnswer = (errorKind: MessageView["error_kind"]): MessageView => ({
  id: "a1",
  turn_id: "trn_1",
  author_kind: "agent",
  author_name: "orchestrator",
  from_agent: null,
  parts: [{ type: "text", text: "The model call failed. Key 1: billing, out of credit." }],
  // A failed turn's answer is complete: the failure is in error_kind, never in the status.
  status: "complete",
  error_kind: errorKind,
  created_at: "2026-01-01T00:00:01Z",
});

describe("MessageList Try again on a failed turn", () => {
  it("re-sends the turn's trigger message when its keys were exhausted", async () => {
    const onSend = vi.fn(() => Promise.resolve());
    const user = userEvent.setup();
    const other: MessageView = { ...trigger, id: "u0", turn_id: "trn_0", parts: [] };
    render(
      <MessageList messages={[other, trigger, failedAnswer("keys_exhausted")]} onSend={onSend} />,
    );

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(onSend).toHaveBeenCalledExactlyOnceWith("Train the baseline model");
  });

  it("disables Try again while the send is pending and shows its error", async () => {
    let fail: ((error: Error) => void) | undefined;
    const onSend = vi.fn(
      () =>
        new Promise<void>((_, reject) => {
          fail = reject;
        }),
    );
    const user = userEvent.setup();
    render(<MessageList messages={[trigger, failedAnswer("keys_exhausted")]} onSend={onSend} />);

    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByRole("button", { name: "Try again" })).toBeDisabled();

    fail?.(new ApiError("conflict", "The agent is busy.", 409));
    expect(await screen.findByRole("alert")).toHaveTextContent("The agent is busy.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("offers no Try again for another failure", () => {
    render(<MessageList messages={[trigger, failedAnswer(null)]} onSend={vi.fn()} />);

    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("offers no Try again without the turn's trigger message", () => {
    render(<MessageList messages={[failedAnswer("keys_exhausted")]} onSend={vi.fn()} />);

    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});

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
