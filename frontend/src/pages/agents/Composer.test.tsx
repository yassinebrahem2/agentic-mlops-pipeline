import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { Composer, type ComposerProps } from "./Composer";

function renderComposer(overrides: Partial<ComposerProps> = {}) {
  const props = {
    agentName: "orchestrator",
    agentDisplayName: "Orchestrator",
    busy: false,
    onSend: vi.fn<ComposerProps["onSend"]>().mockResolvedValue(undefined),
    onStop: vi.fn<ComposerProps["onStop"]>().mockResolvedValue(undefined),
    ...overrides,
  };
  return { ...render(<Composer {...props} />), props };
}

describe("Composer", () => {
  it("shows one disabled Send button when idle and empty", () => {
    renderComposer();
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(send.querySelector("svg")).toHaveClass("lucide-send");
  });

  it("starts at one row with CSS growth, a height cap and internal scrolling", () => {
    renderComposer();
    const input = screen.getByRole("textbox", { name: "Message Orchestrator" });
    expect(input).toHaveAttribute("rows", "1");
    expect(input).toHaveClass(
      "min-h-0",
      "field-sizing-content",
      "h-48",
      "supports-[field-sizing:content]:h-auto",
      "max-h-48",
      "overflow-y-auto",
      "resize-none",
    );
    expect(input).not.toHaveClass("min-h-16");
    expect(input.parentElement).toHaveClass("items-center");
  });

  it("stops a busy turn when empty and sends queued text when typed", async () => {
    const user = userEvent.setup();
    const { props } = renderComposer({ busy: true });
    const stop = screen.getByRole("button", { name: "Stop" });
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(stop.querySelector("svg")).toHaveClass("lucide-square");
    await user.click(stop);
    expect(props.onStop).toHaveBeenCalledOnce();
    await user.type(screen.getByRole("textbox"), "Queue this");
    const send = screen.getByRole("button", { name: "Send" });
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(send.querySelector("svg")).toHaveClass("lucide-send");
    await user.click(send);
    expect(props.onSend).toHaveBeenCalledExactlyOnceWith("Queue this");
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
  });

  it("shows Stop when busy even if composer holds only whitespace", async () => {
    const user = userEvent.setup();
    const { props } = renderComposer({ busy: true });
    const textbox = screen.getByRole("textbox");

    await user.type(textbox, "   ");
    let stop = screen.getByRole("button", { name: "Stop" });
    expect(stop).toBeEnabled();
    expect(stop.querySelector("svg")).toHaveClass("lucide-square");
    await user.click(stop);
    expect(props.onStop).toHaveBeenCalledOnce();

    await user.type(textbox, "{Shift>}{Enter}{/Shift}");
    stop = screen.getByRole("button", { name: "Stop" });
    expect(stop).toBeEnabled();
    expect(stop.querySelector("svg")).toHaveClass("lucide-square");

    await user.type(textbox, "a");
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeEnabled();
    expect(send.querySelector("svg")).toHaveClass("lucide-send");
  });

  it("keeps Shift+Enter as a newline and Enter as send", async () => {
    const user = userEvent.setup();
    const { props } = renderComposer();
    await user.type(screen.getByRole("textbox"), "First{Shift>}{Enter}{/Shift}Second");
    expect(screen.getByRole("textbox")).toHaveValue("First\nSecond");
    expect(props.onSend).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(props.onSend).toHaveBeenCalledExactlyOnceWith("First\nSecond");
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("disables Send for whitespace and while a send is pending", async () => {
    const user = userEvent.setup();
    let resolveSend!: () => void;
    const onSend = vi.fn<ComposerProps["onSend"]>(
      () =>
        new Promise<void>((resolve) => {
          resolveSend = resolve;
        }),
    );
    renderComposer({ onSend });
    await user.type(screen.getByRole("textbox"), "   ");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.type(screen.getByRole("textbox"), "message");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledOnce();
    resolveSend();
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(""));
  });

  it("keeps a failed send's text and clears the error on editing", async () => {
    const user = userEvent.setup();
    renderComposer({
      onSend: vi.fn().mockRejectedValue(new ApiError("send_failed", "Send failed")),
    });
    await user.type(screen.getByRole("textbox"), "Keep this");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Send failed");
    expect(screen.getByRole("textbox")).toHaveValue("Keep this");
    await user.type(screen.getByRole("textbox"), "!");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows stop errors under the input", async () => {
    const user = userEvent.setup();
    renderComposer({
      busy: true,
      onStop: vi.fn().mockRejectedValue(new ApiError("stop_failed", "Stop failed")),
    });
    await user.click(screen.getByRole("button", { name: "Stop" }));
    const error = screen.getByRole("alert");
    expect(error).toHaveTextContent("Stop failed");
    expect(error.previousElementSibling).toContainElement(screen.getByRole("textbox"));
  });

  it("renders notice when turn is stopped by step limit (max_steps) and idle", () => {
    renderComposer({ busy: false, stoppedReason: "max_steps" });
    const notice = screen.getByRole("status");
    expect(notice).toBeInTheDocument();
    expect(notice).toHaveTextContent("Stopped at its step limit: send a message to continue");
    expect(notice).toHaveClass("text-xs", "text-muted-foreground");
  });

  it("renders notice when turn is stopped because context is full (room_below_floor) and idle", () => {
    renderComposer({ busy: false, stoppedReason: "room_below_floor" });
    const notice = screen.getByRole("status");
    expect(notice).toBeInTheDocument();
    expect(notice).toHaveTextContent("Stopped: its context is full");
    expect(notice).toHaveClass("text-xs", "text-muted-foreground");
  });

  it("does not render stopped notice when busy", () => {
    renderComposer({ busy: true, stoppedReason: "max_steps" });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("does not render stopped notice when stoppedReason is empty or unknown", () => {
    renderComposer({ busy: false, stoppedReason: null });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    renderComposer({ busy: false, stoppedReason: "other_reason" });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
