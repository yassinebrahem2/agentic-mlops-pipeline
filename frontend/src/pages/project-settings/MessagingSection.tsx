import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";

type Limits = components["schemas"]["MessagingLimits"];
type LimitName = keyof Limits;
type Draft = Record<LimitName, string>;

const LIMIT_ERROR = "Enter a whole number of at least 1.";

const FIELDS: { name: LimitName; id: string; label: string; help: string }[] = [
  {
    name: "max_hops",
    id: "messaging-max-hops",
    label: "Maximum messages",
    help: "The most messages one exchange between agents can hold. An agent that reaches it is told to report to the user instead of sending another.",
  },
  {
    name: "max_tokens",
    id: "messaging-max-tokens",
    label: "Maximum tokens",
    help: "The most model tokens the agents in one exchange can use while handling its messages. Past it, an agent is told to report to the user instead of sending another.",
  },
];

const messagingKey = (projectId: string) =>
  ["projects", projectId, "settings", "messaging"] as const;

function toDraft(limits: Limits): Draft {
  return { max_hops: String(limits.max_hops), max_tokens: String(limits.max_tokens) };
}

function parseLimit(text: string): number | null {
  const trimmed = text.trim();
  if (!/^[0-9]+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isSafeInteger(value) && value >= 1 ? value : null;
}

function parseDraft(draft: Draft): Limits | null {
  const max_hops = parseLimit(draft.max_hops);
  const max_tokens = parseLimit(draft.max_tokens);
  return max_hops === null || max_tokens === null ? null : { max_hops, max_tokens };
}

export function MessagingSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const params = { path: { project_id: projectId } };
  const limits = useQuery({
    queryKey: messagingKey(projectId),
    queryFn: async () =>
      unwrap(await api.GET("/api/projects/{project_id}/settings/messaging", { params })),
  });
  const save = useMutation({
    mutationFn: async (body: Limits) =>
      unwrap(await api.PUT("/api/projects/{project_id}/settings/messaging", { params, body })),
    onSuccess: (saved) => {
      queryClient.setQueryData(messagingKey(projectId), saved);
    },
  });
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="messaging-title">
      <h2 id="messaging-title" className="text-lg font-semibold">
        Agent messages
      </h2>
      {limits.isPending && <p role="status">Loading message limits...</p>}
      {limits.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(limits.error)}
        </p>
      )}
      {limits.data && (
        // Keyed on the stored values so the form re-seeds when they change.
        <MessagingForm
          key={JSON.stringify(limits.data)}
          stored={limits.data}
          pending={save.isPending}
          onSave={(body) => {
            save.mutate(body);
          }}
        />
      )}
      {save.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(save.error)}
        </p>
      )}
    </section>
  );
}

function MessagingForm({
  stored,
  pending,
  onSave,
}: {
  stored: Limits;
  pending: boolean;
  onSave: (limits: Limits) => void;
}) {
  const [draft, setDraft] = useState(() => toDraft(stored));
  const [attempted, setAttempted] = useState(false);
  const unchanged = FIELDS.every(({ name }) => draft[name] === String(stored[name]));
  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    setAttempted(true);
    const limits = parseDraft(draft);
    if (limits !== null) onSave(limits);
  };
  return (
    <form className="space-y-4" noValidate onSubmit={submit}>
      {FIELDS.map(({ name, id, label, help }) => {
        const invalid = attempted && parseLimit(draft[name]) === null;
        return (
          <div key={name} className="space-y-2">
            <Label htmlFor={id}>{label}</Label>
            <Input
              id={id}
              type="number"
              inputMode="numeric"
              min={1}
              step={1}
              className="w-full sm:w-64"
              disabled={pending}
              aria-invalid={invalid}
              aria-describedby={`${id}-help${invalid ? ` ${id}-error` : ""}`}
              value={draft[name]}
              onChange={(event) => {
                setDraft({ ...draft, [name]: event.target.value });
              }}
            />
            <p id={`${id}-help`} className="text-sm text-muted-foreground">
              {help}
            </p>
            {invalid && (
              <p id={`${id}-error`} className="text-sm text-destructive">
                {LIMIT_ERROR}
              </p>
            )}
          </div>
        );
      })}
      <Button type="submit" disabled={unchanged || pending}>
        {pending ? "Saving..." : "Save"}
      </Button>
    </form>
  );
}
