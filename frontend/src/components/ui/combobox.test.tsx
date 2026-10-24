import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Combobox } from "./combobox";

function ComboboxHarness(props: {
  initialValue?: string;
  options?: { value: string; label?: string; detail?: string }[];
  loading?: boolean;
  emptyText?: string;
  label?: string;
  placeholder?: string;
  onValueChange?: (val: string) => void;
}) {
  const [val, setVal] = useState(props.initialValue ?? "");
  return (
    <Combobox
      label={props.label ?? "Select item"}
      value={val}
      onValueChange={(next) => {
        setVal(next);
        props.onValueChange?.(next);
      }}
      options={
        props.options ?? [
          { value: "apple", label: "Apple", detail: "Fruit" },
          { value: "banana", label: "Banana", detail: "Fruit" },
          { value: "carrot", label: "Carrot", detail: "Vegetable" },
        ]
      }
      loading={props.loading}
      emptyText={props.emptyText}
      placeholder={props.placeholder}
    />
  );
}

describe("Combobox", () => {
  it("keeps matching typed text on Enter until an arrow key highlights an option", async () => {
    const user = userEvent.setup();
    render(<ComboboxHarness />);
    const input = screen.getByRole("combobox");

    await user.type(input, "app");
    await user.keyboard("{Enter}");
    expect(input).toHaveValue("app");
    expect(input).not.toHaveAttribute("aria-activedescendant");
    expect(screen.getByRole("option")).toHaveAttribute("aria-selected", "false");

    await user.keyboard("{ArrowDown}");
    expect(input).toHaveAttribute("aria-activedescendant");
    await user.keyboard("l");
    expect(input).not.toHaveAttribute("aria-activedescendant");
    await user.hover(screen.getByRole("option"));
    expect(input).not.toHaveAttribute("aria-activedescendant");
    await user.keyboard("{Enter}");
    expect(input).toHaveValue("appl");
    expect(input).toHaveFocus();
  });

  it("has the required ARIA attributes", async () => {
    const user = userEvent.setup();
    render(<ComboboxHarness label="Fruit picker" />);

    const input = screen.getByRole("combobox", { name: "Fruit picker" });
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-expanded", "false");

    await user.click(input);
    expect(input).toHaveAttribute("aria-expanded", "true");

    const listboxId = input.getAttribute("aria-controls");
    expect(listboxId).toBeTruthy();

    const listbox = screen.getByRole("listbox");
    expect(listbox).toHaveAttribute("id", listboxId);

    const options = screen.getAllByRole("option");
    expect(options.length).toBe(3);

    // Press ArrowDown to highlight the first option
    await user.keyboard("{ArrowDown}");
    const activeDescendant = input.getAttribute("aria-activedescendant");
    expect(activeDescendant).toBeTruthy();
    expect(options[0]).toHaveAttribute("id", activeDescendant);
    expect(options[0]).toHaveAttribute("aria-selected", "true");
  });

  it("typing filters matching anywhere in value or label case-insensitively", async () => {
    const user = userEvent.setup();
    render(<ComboboxHarness label="Fruit picker" />);

    const input = screen.getByRole("combobox", { name: "Fruit picker" });
    await user.type(input, "an");

    // "banana" matches "an"
    expect(screen.getByRole("option", { name: /banana/i })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /apple/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /carrot/i })).not.toBeInTheDocument();
  });

  it("the arrow keys and Enter pick, and Escape closes", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<ComboboxHarness label="Fruit picker" onValueChange={onValueChange} />);

    const input = screen.getByRole("combobox", { name: "Fruit picker" });
    await user.click(input);
    expect(input).toHaveFocus();

    // Arrow down to first, then second
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: /banana/i })).toHaveAttribute("aria-selected", "true");

    // Arrow up back to first
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("option", { name: /apple/i })).toHaveAttribute("aria-selected", "true");

    // Enter picks the highlighted option
    await user.keyboard("{Enter}");
    expect(input).toHaveValue("apple");
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(input).toHaveFocus();

    // Reopen and test Escape
    await user.click(input);
    expect(input).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(input).toHaveFocus();
  });

  it("clicking an option picks it", async () => {
    const user = userEvent.setup();
    render(<ComboboxHarness label="Fruit picker" />);

    const input = screen.getByRole("combobox", { name: "Fruit picker" });
    await user.click(input);

    const carrotOption = screen.getByRole("option", { name: /carrot/i });
    await user.click(carrotOption);

    expect(input).toHaveValue("carrot");
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(input).toHaveFocus();
  });

  it("unmatched typed text is kept as the value", async () => {
    const user = userEvent.setup();
    render(<ComboboxHarness label="Custom picker" emptyText="No match" />);

    const input = screen.getByRole("combobox", { name: "Custom picker" });
    await user.type(input, "https://github.com/org/repo");

    expect(input).toHaveValue("https://github.com/org/repo");
    expect(screen.getByText("No match")).toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toHaveLength(0);

    // Enter does not replace typed text
    await user.keyboard("{Enter}");
    expect(input).toHaveValue("https://github.com/org/repo");
  });

  it("the loading state still takes typing and displays loading row", async () => {
    const user = userEvent.setup();
    render(<ComboboxHarness label="Async picker" loading={true} options={[]} />);

    const input = screen.getByRole("combobox", { name: "Async picker" });
    expect(input).not.toBeDisabled();

    await user.type(input, "my-query");
    expect(input).toHaveValue("my-query");

    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });
});
