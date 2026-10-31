import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { api, errorText, unwrap } from "@/lib/api";
import { formatTime } from "@/lib/format";
import { NoteEditor } from "./NoteEditor";
import { NOTE_TYPE_LABELS, type NoteView } from "./noteTypes";
import { notesQueryKey } from "./useNotes";

export function NoteRow({ projectId, note }: { projectId: string; note: NoteView }) {
  const [editing, setEditing] = useState(false);

  return (
    <li data-testid={`note-${note.id}`}>
      <Card size="sm">
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{NOTE_TYPE_LABELS[note.type]}</Badge>
            {!editing && <span className="break-words">{note.title}</span>}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {editing ? (
            <NoteEditor
              projectId={projectId}
              note={note}
              onDone={() => {
                setEditing(false);
              }}
            />
          ) : (
            <>
              {/* A text node, never HTML: notes are written by agents and people. */}
              <p className="whitespace-pre-wrap break-words text-sm">{note.text}</p>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-muted-foreground">
                  {note.author} · updated {formatTime(note.updated)}
                  {note.source !== null && ` · ${note.source}`}
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setEditing(true);
                    }}
                  >
                    <Pencil className="size-4" />
                    Edit
                  </Button>
                  <DeleteNoteDialog projectId={projectId} note={note} />
                </div>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </li>
  );
}

function DeleteNoteDialog({ projectId, note }: { projectId: string; note: NoteView }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const remove = useMutation({
    mutationFn: async () => {
      unwrap(
        await api.DELETE("/api/projects/{project_id}/notes/{note_id}", {
          params: { path: { project_id: projectId, note_id: note.id } },
        }),
      );
    },
    onSuccess: async () => {
      setOpen(false);
      await queryClient.invalidateQueries({ queryKey: notesQueryKey(projectId) });
    },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        remove.reset();
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="destructive" size="sm">
          <Trash2 className="size-4" />
          Delete
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete this note?</DialogTitle>
          <DialogDescription>
            &ldquo;{note.title}&rdquo; is removed from the project&apos;s knowledge for every agent.
            This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        {remove.isError && <p className="text-destructive">{errorText(remove.error)}</p>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            disabled={remove.isPending}
            onClick={() => {
              remove.mutate();
            }}
          >
            Delete note
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
