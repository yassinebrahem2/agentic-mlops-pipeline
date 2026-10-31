import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, fieldErrors, unwrap } from "@/lib/api";

type Settings = components["schemas"]["MonitoringSettings"];
type NumberName = Exclude<keyof Settings, "timezone">;
type Draft = Record<NumberName, string>;

const NUMBER_ERROR = "Enter a whole number.";

const FIELDS: { name: NumberName; label: string; help: string }[] = [
  {
    name: "collection_interval_s",
    label: "Collection interval (seconds)",
    help: "How often results are collected and checks are evaluated.",
  },
  {
    name: "grouping_window_minutes",
    label: "Grouping window (minutes)",
    help: "Signals raised within this window are grouped into one incident.",
  },
  {
    name: "reopen_window_hours",
    label: "Reopen window (hours)",
    help: "A signal that returns within this window reopens a resolved incident.",
  },
  {
    name: "recent_change_window_hours",
    label: "Recent change window (hours)",
    help: "Changes made within this window before an incident are listed as suspects.",
  },
  {
    name: "escalation_timeout_minutes",
    label: "Escalation timeout (minutes)",
    help: "How long before the next person on call is notified.",
  },
  {
    name: "check_grace_minutes",
    label: "Check grace (minutes)",
    help: "How late a check result may arrive before it counts as missing.",
  },
  {
    name: "second_deadline_minutes",
    label: "Second deadline (minutes)",
    help: "The extra time after the first deadline for a missing result.",
  },
  {
    name: "parser_disable_after",
    label: "Disable a parser after (failures)",
    help: "How many failures in a row disable a results parser.",
  },
  {
    name: "evaluation_max_age_hours",
    label: "Evaluation maximum age (hours)",
    help: "The oldest evaluation result that is still used.",
  },
  {
    name: "circuit_breaker_fixes",
    label: "Circuit breaker fixes",
    help: "How many fixes within the window below open the circuit breaker.",
  },
  {
    name: "circuit_breaker_window_days",
    label: "Circuit breaker window (days)",
    help: "The period in which fixes are counted for the circuit breaker.",
  },
];

const monitoringKey = (projectId: string) =>
  ["projects", projectId, "monitoring-settings"] as const;

function toDraft(stored: Settings): Draft {
  const draft = {} as Draft;
  for (const { name } of FIELDS) draft[name] = String(stored[name]);
  return draft;
}

function parseWhole(text: string): number | null {
  const trimmed = text.trim();
  if (!/^-?[0-9]+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isSafeInteger(value) ? value : null;
}

/** Every field parsed, or null when one is not a whole number (the form marks which). */
function toSettings(draft: Draft, timezone: string): Settings | null {
  const parsed = {} as Record<NumberName, number>;
  for (const { name } of FIELDS) {
    const value = parseWhole(draft[name]);
    if (value === null) return null;
    parsed[name] = value;
  }
  // An empty time zone means the server's own.
  const trimmed = timezone.trim();
  return { ...parsed, timezone: trimmed === "" ? null : trimmed };
}

export function MonitoringSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const params = { path: { project_id: projectId } };
  const settings = useQuery({
    queryKey: monitoringKey(projectId),
    queryFn: async () =>
      unwrap(await api.GET("/api/projects/{project_id}/monitoring-settings", { params })),
  });
  const save = useMutation({
    mutationFn: async (body: Settings) =>
      unwrap(await api.PUT("/api/projects/{project_id}/monitoring-settings", { params, body })),
    onSuccess: (saved) => {
      queryClient.setQueryData(monitoringKey(projectId), saved);
    },
  });
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="monitoring-title">
      <h2 id="monitoring-title" className="text-lg font-semibold">
        Monitoring
      </h2>
      <p className="text-sm text-muted-foreground">
        The minimum priority for opening GitHub issues is set in the GitHub issues section.
      </p>
      <QueryStatus query={settings} loading="Loading monitoring settings..." />
      {settings.data && (
        // Keyed on the stored values so the form re-seeds after a save.
        <MonitoringForm
          key={JSON.stringify(settings.data)}
          stored={settings.data}
          pending={save.isPending}
          saved={save.isSuccess}
          error={save.error}
          onSave={(body) => {
            save.mutate(body);
          }}
        />
      )}
    </section>
  );
}

interface FormProps {
  stored: Settings;
  pending: boolean;
  saved: boolean;
  error: unknown;
  onSave: (settings: Settings) => void;
}

function MonitoringForm({ stored, pending, saved, error, onSave }: FormProps) {
  const timezoneId = useId();
  const [draft, setDraft] = useState(() => toDraft(stored));
  const [timezone, setTimezone] = useState(stored.timezone ?? "");
  const [attempted, setAttempted] = useState(false);
  const parsed = toSettings(draft, timezone);
  const dirty = JSON.stringify(parsed) !== JSON.stringify(stored);
  const serverErrors = fieldErrors(error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setAttempted(true);
    if (parsed !== null) onSave(parsed);
  }

  return (
    <form className="space-y-4" noValidate onSubmit={submit}>
      {FIELDS.map(({ name, label, help }) => {
        const id = `monitoring-${name}`;
        const message =
          attempted && parseWhole(draft[name]) === null ? NUMBER_ERROR : serverErrors[name];
        return (
          <div key={name} className="space-y-2">
            <Label htmlFor={id}>{label}</Label>
            <Input
              id={id}
              type="number"
              inputMode="numeric"
              className="w-full sm:w-64"
              disabled={pending}
              aria-invalid={message !== undefined}
              aria-describedby={`${id}-help`}
              value={draft[name]}
              onChange={(event) => {
                setDraft({ ...draft, [name]: event.target.value });
              }}
            />
            <p id={`${id}-help`} className="text-sm text-muted-foreground">
              {help}
            </p>
            {message && <p className="text-sm text-destructive">{message}</p>}
          </div>
        );
      })}
      <div className="space-y-2">
        <Label htmlFor={timezoneId}>Time zone</Label>
        <Input
          id={timezoneId}
          className="w-full sm:w-64"
          disabled={pending}
          aria-invalid={serverErrors.timezone !== undefined}
          aria-describedby={`${timezoneId}-help`}
          value={timezone}
          onChange={(event) => {
            setTimezone(event.target.value);
          }}
        />
        <p id={`${timezoneId}-help`} className="text-sm text-muted-foreground">
          The time zone monitoring uses. Leave it empty to use the server&apos;s time zone.
        </p>
        {serverErrors.timezone && (
          <p className="text-sm text-destructive">{serverErrors.timezone}</p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={pending || !dirty}>
          {pending ? "Saving..." : "Save monitoring"}
        </Button>
        {saved && !dirty && <p role="status">Saved.</p>}
      </div>
      {error !== null && error !== undefined && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      )}
    </form>
  );
}
