import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Switch } from "./switch";

function Harness({ disabled, onChange }: { disabled?: boolean; onChange?: (next: boolean) => void }) {
  const [checked, setChecked] = useState(false);
  return (
    <Switch
      aria-label="Network access"
      checked={checked}
      disabled={disabled}
      onCheckedChange={(next) => {
        setChecked(next);
        onChange?.(next);
      }}
    />
  );
}

describe("Switch", () => {
  it("is a switch with its checked state and accessible name", () => {
    render(<Harness />);
    const toggle = screen.getByRole("switch", { name: "Network access" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("toggles by click", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const toggle = screen.getByRole("switch", { name: "Network access" });

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(onChange.mock.calls).toEqual([[true], [false]]);
  });

  it("toggles by the Space key", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const toggle = screen.getByRole("switch", { name: "Network access" });

    toggle.focus();
    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });

  it("does nothing while disabled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness disabled onChange={onChange} />);
    const toggle = screen.getByRole("switch", { name: "Network access" });

    expect(toggle).toBeDisabled();
    await user.click(toggle);
    toggle.focus();
    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(onChange).not.toHaveBeenCalled();
  });
});
