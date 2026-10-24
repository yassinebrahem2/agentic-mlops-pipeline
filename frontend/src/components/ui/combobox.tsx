import * as React from "react";
import { useId, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { Popover } from "radix-ui";
import { cn } from "cn";
import { Input } from "@/components/ui/input";

export interface ComboboxOption {
  value: string;
  label?: string;
  detail?: string;
}

export interface ComboboxProps {
  id?: string;
  label: string; // accessible name
  value: string; // the field's text, which is the value
  onValueChange: (value: string) => void;
  options: ComboboxOption[];
  loading?: boolean; // options still loading: the field stays usable
  placeholder?: string;
  emptyText?: string; // shown when no option matches
  disabled?: boolean;
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
}

export function Combobox({
  id,
  label,
  value,
  onValueChange,
  options,
  loading = false,
  placeholder,
  emptyText = "No options found",
  disabled = false,
  onKeyDown,
}: ComboboxProps): React.JSX.Element {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const listboxId = `${inputId}-listbox`;

  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);

  const query = value.trim().toLowerCase();
  const filteredOptions = useMemo(() => {
    if (!query) return options;
    return options.filter((opt) => {
      const valMatch = opt.value.toLowerCase().includes(query);
      const labelMatch = opt.label?.toLowerCase().includes(query);
      return valMatch || Boolean(labelMatch);
    });
  }, [options, query]);

  const effectiveHighlightedIndex =
    highlightedIndex >= filteredOptions.length ? -1 : highlightedIndex;

  const handleSelect = (option: ComboboxOption) => {
    onValueChange(option.value);
    setOpen(false);
    setHighlightedIndex(-1);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        setHighlightedIndex(0);
      } else if (filteredOptions.length > 0) {
        setHighlightedIndex((prev) =>
          prev < 0 ? 0 : prev < filteredOptions.length - 1 ? prev + 1 : prev,
        );
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        setHighlightedIndex(filteredOptions.length > 0 ? filteredOptions.length - 1 : 0);
      } else if (filteredOptions.length > 0) {
        setHighlightedIndex((prev) => (prev > 0 ? prev - 1 : 0));
      }
    } else if (e.key === "Enter") {
      if (open && effectiveHighlightedIndex >= 0 && filteredOptions[effectiveHighlightedIndex]) {
        e.preventDefault();
        handleSelect(filteredOptions[effectiveHighlightedIndex]);
      } else {
        onKeyDown?.(e);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
        setHighlightedIndex(-1);
      }
    } else if (e.key === "Tab") {
      setOpen(false);
      setHighlightedIndex(-1);
    } else {
      onKeyDown?.(e);
    }
  };

  const activeOptionId =
    open && effectiveHighlightedIndex >= 0 && filteredOptions[effectiveHighlightedIndex]
      ? `${listboxId}-option-${effectiveHighlightedIndex}`
      : undefined;

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Anchor asChild>
        <Input
          id={inputId}
          role="combobox"
          aria-label={label}
          aria-expanded={open}
          aria-haspopup="listbox"
          aria-autocomplete="list"
          aria-controls={open ? listboxId : undefined}
          aria-activedescendant={activeOptionId}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(e) => {
            onValueChange(e.target.value);
            setOpen(true);
            setHighlightedIndex(-1);
          }}
          onFocus={() => {
            setOpen(true);
          }}
          onClick={() => {
            setOpen(true);
          }}
          onKeyDown={handleKeyDown}
        />
      </Popover.Anchor>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={4}
          onOpenAutoFocus={(e) => {
            e.preventDefault();
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
          }}
          className="z-50 max-h-60 w-[var(--radix-popover-anchor-width,16rem)] min-w-[var(--radix-popover-anchor-width,16rem)] overflow-x-hidden overflow-y-auto rounded-md border bg-popover p-1 text-popover-foreground shadow-md outline-none"
        >
          <div role="listbox" id={listboxId} aria-label={label}>
            {loading ? (
              <div className="flex items-center justify-center gap-2 py-3 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                <span>Loading...</span>
              </div>
            ) : filteredOptions.length === 0 ? (
              <div className="py-3 text-center text-sm text-muted-foreground">{emptyText}</div>
            ) : (
              filteredOptions.map((opt, index) => {
                const isHighlighted = index === effectiveHighlightedIndex;
                const optionId = `${listboxId}-option-${index}`;
                return (
                  <div
                    key={opt.value}
                    id={optionId}
                    role="option"
                    aria-selected={isHighlighted}
                    className={cn(
                      "flex cursor-pointer items-center justify-between rounded-sm px-2.5 py-1.5 text-sm select-none",
                      isHighlighted && "bg-accent text-accent-foreground",
                    )}
                    onMouseDown={(e) => {
                      e.preventDefault();
                    }}
                    onClick={() => {
                      handleSelect(opt);
                    }}
                  >
                    <span className="truncate">{opt.label ?? opt.value}</span>
                    {opt.detail && (
                      <span className="ml-2 max-w-[200px] truncate text-xs text-muted-foreground">
                        {opt.detail}
                      </span>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
