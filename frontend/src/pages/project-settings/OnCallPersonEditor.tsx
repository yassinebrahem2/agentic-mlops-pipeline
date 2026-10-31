import { useId } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export type OnCall = components["schemas"]["OnCall"];
type Day = OnCall["hours"]["days"][number];
type Channel = OnCall["contacts"][number]["channel"];

/** A row being edited: the stored person plus a client-only key, so reordering keeps each row's state. */
export interface Draft {
  key: string;
  person: Omit<OnCall, "escalation_order">;
}

const DAY_LABELS: Record<Day, string> = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};
const DAYS = Object.keys(DAY_LABELS).filter((day): day is Day => day in DAY_LABELS);

const CHANNEL_LABELS: Record<Channel, string> = { discord: "Discord", email: "Email" };
const CHANNELS = Object.keys(CHANNEL_LABELS).filter(
  (channel): channel is Channel => channel in CHANNEL_LABELS,
);

function isChannel(value: string): value is Channel {
  return CHANNELS.some((channel) => channel === value);
}

interface Props {
  position: number;
  count: number;
  person: Draft["person"];
  errors: string[];
  disabled: boolean;
  onChange: (person: Draft["person"]) => void;
  onMove: (offset: -1 | 1) => void;
  onRemove: () => void;
}

export function OnCallPersonEditor({
  position,
  count,
  person,
  errors,
  disabled,
  onChange,
  onMove,
  onRemove,
}: Props) {
  const ids = { name: useId(), zone: useId(), start: useId(), end: useId() };
  const number = position + 1;
  const { hours, contacts } = person;

  const setContact = (index: number, next: Partial<(typeof contacts)[number]>) => {
    onChange({
      ...person,
      contacts: contacts.map((contact, i) => (i === index ? { ...contact, ...next } : contact)),
    });
  };
  const toggleDay = (day: Day, on: boolean) => {
    // Kept in week order so the saved list does not depend on the order of the clicks.
    const days = DAYS.filter((d) => (d === day ? on : hours.days.includes(d)));
    onChange({ ...person, hours: { ...hours, days } });
  };

  return (
    <fieldset
      aria-label={`Person ${String(number)}`}
      className="space-y-4 rounded-md border p-4"
      disabled={disabled}
    >
      <div className="flex flex-wrap gap-4">
        <div className="space-y-2">
          <Label htmlFor={ids.name}>Name</Label>
          <Input
            id={ids.name}
            className="w-full sm:w-56"
            value={person.name}
            onChange={(event) => {
              onChange({ ...person, name: event.target.value });
            }}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={ids.zone}>Time zone</Label>
          <Input
            id={ids.zone}
            className="w-full sm:w-56"
            value={person.timezone}
            onChange={(event) => {
              onChange({ ...person, timezone: event.target.value });
            }}
          />
        </div>
      </div>
      <div className="space-y-2">
        <p className="text-sm font-medium">Days on call</p>
        <div className="flex flex-wrap gap-4">
          {DAYS.map((day) => (
            <label key={day} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={hours.days.includes(day)}
                onChange={(event) => {
                  toggleDay(day, event.target.checked);
                }}
              />
              {DAY_LABELS[day]}
            </label>
          ))}
        </div>
        <div className="flex flex-wrap gap-4">
          <div className="space-y-2">
            <Label htmlFor={ids.start}>Start</Label>
            <Input
              id={ids.start}
              type="time"
              className="w-36"
              value={hours.start}
              onChange={(event) => {
                onChange({ ...person, hours: { ...hours, start: event.target.value } });
              }}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor={ids.end}>End</Label>
            <Input
              id={ids.end}
              type="time"
              className="w-36"
              value={hours.end}
              onChange={(event) => {
                onChange({ ...person, hours: { ...hours, end: event.target.value } });
              }}
            />
          </div>
        </div>
      </div>
      <div className="space-y-2">
        <p className="text-sm font-medium">Contacts</p>
        {contacts.map((contact, index) => {
          const n = String(index + 1);
          return (
            <div key={index} className="flex flex-wrap items-center gap-2">
              <select
                aria-label={`Contact ${n} channel`}
                className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                value={contact.channel}
                onChange={(event) => {
                  if (isChannel(event.target.value))
                    setContact(index, { channel: event.target.value });
                }}
              >
                {CHANNELS.map((channel) => (
                  <option key={channel} value={channel}>
                    {CHANNEL_LABELS[channel]}
                  </option>
                ))}
              </select>
              <Input
                aria-label={`Contact ${n} address`}
                className="w-full sm:w-72"
                value={contact.address}
                onChange={(event) => {
                  setContact(index, { address: event.target.value });
                }}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                aria-label={`Remove contact ${n}`}
                onClick={() => {
                  onChange({ ...person, contacts: contacts.filter((_, i) => i !== index) });
                }}
              >
                Remove
              </Button>
            </div>
          );
        })}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => {
            onChange({ ...person, contacts: [...contacts, { channel: "discord", address: "" }] });
          }}
        >
          Add contact
        </Button>
      </div>
      {errors.length > 0 && (
        <ul className="space-y-1 text-sm text-destructive">
          {errors.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label={`Move person ${String(number)} up`}
          disabled={position === 0}
          onClick={() => {
            onMove(-1);
          }}
        >
          Move up
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label={`Move person ${String(number)} down`}
          disabled={position === count - 1}
          onClick={() => {
            onMove(1);
          }}
        >
          Move down
        </Button>
        <Button
          type="button"
          variant="destructive"
          size="sm"
          aria-label={`Remove person ${String(number)}`}
          onClick={onRemove}
        >
          Remove
        </Button>
      </div>
    </fieldset>
  );
}
