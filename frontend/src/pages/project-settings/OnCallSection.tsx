import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Button } from "@/components/ui/button";
import { api, errorText, fieldErrors, unwrap } from "@/lib/api";
import { DAYS, OnCallPersonEditor, type Draft, type OnCall } from "./OnCallPersonEditor";

type OnCallPerson = components["schemas"]["OnCallPerson"];

const onCallKey = (projectId: string) => ["projects", projectId, "on-call"] as const;

/** Escalation order is the position in the list, so reordering rows is all it takes. */
function toPayload(drafts: Draft[]): OnCall[] {
  return drafts.map(({ person }, index) => ({ ...person, escalation_order: index + 1 }));
}

function fromStored(stored: OnCallPerson[]): Draft[] {
  return [...stored]
    .sort((a, b) => a.escalation_order - b.escalation_order)
    .map(({ id, name, contacts, hours, timezone }) => ({
      key: id,
      person: { name, contacts, hours, timezone },
    }));
}

export function OnCallSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const params = { path: { project_id: projectId } };
  const people = useQuery({
    queryKey: onCallKey(projectId),
    queryFn: async () => unwrap(await api.GET("/api/projects/{project_id}/on-call", { params })),
  });
  const save = useMutation({
    mutationFn: async (body: OnCall[]) =>
      unwrap(await api.PUT("/api/projects/{project_id}/on-call", { params, body })),
    onSuccess: (saved) => {
      queryClient.setQueryData(onCallKey(projectId), saved);
    },
  });
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="on-call-title">
      <h2 id="on-call-title" className="text-lg font-semibold">
        On-call people
      </h2>
      <p className="text-sm text-muted-foreground">
        People are notified in the order listed here. Saving replaces the whole list, and a newly
        approved handoff replaces the list too.
      </p>
      <QueryStatus query={people} loading="Loading on-call people..." />
      {people.data && (
        // Keyed on the stored list so the editor re-seeds after a save.
        <OnCallForm
          key={JSON.stringify(people.data)}
          stored={people.data}
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
  stored: OnCallPerson[];
  pending: boolean;
  saved: boolean;
  error: unknown;
  onSave: (people: OnCall[]) => void;
}

function OnCallForm({ stored, pending, saved, error, onSave }: FormProps) {
  const [drafts, setDrafts] = useState(() => fromStored(stored));
  const nextKey = useRef(0);
  const dirty = JSON.stringify(toPayload(drafts)) !== JSON.stringify(toPayload(fromStored(stored)));
  const serverErrors = fieldErrors(error);

  const update = (index: number, person: Draft["person"]) => {
    setDrafts(drafts.map((draft, i) => (i === index ? { ...draft, person } : draft)));
  };
  const move = (index: number, offset: -1 | 1) => {
    const next = [...drafts];
    const [moved] = next.splice(index, 1);
    if (moved) next.splice(index + offset, 0, moved);
    setDrafts(next);
  };
  const add = () => {
    nextKey.current += 1;
    setDrafts([
      ...drafts,
      {
        key: `new-${String(nextKey.current)}`,
        person: {
          name: "",
          contacts: [{ channel: "discord", address: "" }],
          hours: {
            days: DAYS,
            start: "09:00",
            end: "17:00",
          },
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        },
      },
    ]);
  };
  // The server names a nested field by its path, e.g. "1.hours.days" for the second person.
  const errorsFor = (index: number) =>
    Object.entries(serverErrors)
      .filter(([field]) => field.startsWith(`${String(index)}.`))
      .map(([field, message]) => `${field.slice(String(index).length + 1)}: ${message}`);

  return (
    <div className="space-y-4">
      {drafts.length === 0 && <p>Nobody is on call yet.</p>}
      {drafts.map((draft, index) => (
        <OnCallPersonEditor
          key={draft.key}
          position={index}
          count={drafts.length}
          person={draft.person}
          errors={errorsFor(index)}
          disabled={pending}
          onChange={(person) => {
            update(index, person);
          }}
          onMove={(offset) => {
            move(index, offset);
          }}
          onRemove={() => {
            setDrafts(drafts.filter((_, i) => i !== index));
          }}
        />
      ))}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" variant="outline" disabled={pending} onClick={add}>
          Add person
        </Button>
        <Button
          type="button"
          disabled={pending || !dirty}
          onClick={() => {
            onSave(toPayload(drafts));
          }}
        >
          {pending ? "Saving..." : "Save on-call"}
        </Button>
        {saved && !dirty && <p role="status">Saved.</p>}
      </div>
      {error !== null && error !== undefined && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      )}
    </div>
  );
}
