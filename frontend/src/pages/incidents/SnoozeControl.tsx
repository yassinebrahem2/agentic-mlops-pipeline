import { useId, useState, type SubmitEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatTime } from "@/lib/format";
import { ActionError } from "./ActionError";
import type { IncidentView } from "./labels";
import { useSnooze } from "./useIncidentActions";

/** Shared by the Inbox row and the incident page: a person picks the time the snooze ends. */
export function SnoozeControl({
  projectId,
  incident,
}: {
  projectId: string;
  incident: IncidentView;
}) {
  const inputId = useId();
  const [open, setOpen] = useState(false);
  const [until, setUntil] = useState("");
  const snooze = useSnooze({ projectId, incidentId: incident.id });
  const when = new Date(until);
  const ready = !Number.isNaN(when.getTime());
  // The backend keeps snoozed_until after it passes; only a snooze still in force is worth saying.
  const snoozed = incident.snoozed_until && new Date(incident.snoozed_until).getTime() > Date.now();

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready) return;
    snooze.mutate(when, {
      onSuccess: () => {
        setOpen(false);
        setUntil("");
      },
    });
  };

  return (
    <div className="space-y-2">
      {snoozed && (
        <p className="text-xs text-muted-foreground">
          Snoozed until {formatTime(incident.snoozed_until)}
        </p>
      )}
      {open ? (
        <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
          <div className="space-y-1">
            <Label htmlFor={inputId}>Snooze until</Label>
            <Input
              id={inputId}
              type="datetime-local"
              value={until}
              disabled={snooze.isPending}
              onChange={(event) => {
                setUntil(event.target.value);
              }}
            />
          </div>
          <Button type="submit" size="sm" disabled={!ready || snooze.isPending}>
            Snooze until chosen time
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => {
              setOpen(false);
              snooze.reset();
            }}
          >
            Cancel
          </Button>
        </form>
      ) : (
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            setOpen(true);
          }}
        >
          Snooze
        </Button>
      )}
      <ActionError error={snooze.error} />
    </div>
  );
}
