import { useId, useState, type SubmitEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { api, errorText, unwrap } from "@/lib/api";
import {
  changedFields,
  NOTE_TYPE_LABELS,
  NOTE_TYPES,
  type NoteType,
  type NoteView,
} from "./noteTypes";
import { notesQueryKey } from "./useNotes";

type NoteUpdate = components["schemas"]["NoteUpdate"];

type Props = {
  projectId: string;
  note: NoteView;
  onDone: () => void;
};

export function NoteEditor({ projectId, note, onDone }: Props) {
  const queryClient = useQueryClient();
  const titleId = useId();
  const textId = useId();
  const typeId = useId();
  const [type, setType] = useState<NoteType>(note.type);
  const [title, setTitle] = useState(note.title);
  const [text, setText] = useState(note.text);

  const patch = changedFields(note, { type, title, text });
  const unchanged = Object.keys(patch).length === 0;

  const save = useMutation({
    mutationFn: async (body: NoteUpdate) =>
      unwrap(
        await api.PATCH("/api/projects/{project_id}/notes/{note_id}", {
          params: { path: { project_id: projectId, note_id: note.id } },
          body,
        }),
      ),
    onSuccess: (saved) => {
      // Show the saved note at once; the note.saved event then refreshes the lists.
      queryClient.setQueriesData<{ notes: NoteView[] }>(
        { queryKey: notesQueryKey(projectId) },
        (data) =>
          data && { ...data, notes: data.notes.map((n) => (n.id === saved.id ? saved : n)) },
      );
      onDone();
    },
  });

  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    if (!unchanged) save.mutate(patch);
  };

  return (
    <form onSubmit={submit} className="space-y-3">
      <div className="space-y-1.5">
        <Label htmlFor={typeId}>Note type</Label>
        <Select
          value={type}
          onValueChange={(value) => {
            const picked = NOTE_TYPES.find((t) => t === value);
            if (picked) setType(picked);
          }}
        >
          <SelectTrigger id={typeId} className="w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {NOTE_TYPES.map((t) => (
              <SelectItem key={t} value={t}>
                {NOTE_TYPE_LABELS[t]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={titleId}>Title</Label>
        <Input
          id={titleId}
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
          }}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={textId}>Text</Label>
        <Textarea
          id={textId}
          rows={6}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
          }}
        />
      </div>
      {save.isError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(save.error)}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" disabled={unchanged || save.isPending}>
          {save.isPending && <Loader2 className="size-4 animate-spin" />}
          Save
        </Button>
        <Button type="button" variant="outline" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
