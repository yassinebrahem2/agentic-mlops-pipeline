import { useState, type SubmitEvent } from "react";
import { useParams } from "react-router";
import { AlertCircle, BookOpen, Loader2, Search } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { errorText } from "@/lib/api";
import { fromSelectValue, SELECT_NONE, toSelectValue } from "@/lib/selectNone";
import { NoteRow } from "./NoteRow";
import { NOTE_TYPE_LABELS, NOTE_TYPES, type NoteType } from "./noteTypes";
import { NOTES_PAGE_SIZE, useNotes } from "./useNotes";

export function KnowledgePage() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return null;
  return <Knowledge projectId={projectId} />;
}

function Knowledge({ projectId }: { projectId: string }) {
  // The search box is applied on submit, so typing does not query on every keystroke.
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [type, setType] = useState<NoteType | undefined>(undefined);
  // Back to one page whenever the search or filter changes.
  const [limit, setLimit] = useState(NOTES_PAGE_SIZE);

  const query = useNotes(projectId, { q, type, limit });
  const notes = query.data?.notes ?? [];
  const filtered = q !== "" || type !== undefined;

  const applySearch = (event: SubmitEvent) => {
    event.preventDefault();
    setQ(draft.trim());
    setLimit(NOTES_PAGE_SIZE);
  };

  return (
    <div className="container max-w-4xl py-6 space-y-6">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <BookOpen className="size-6 text-primary" />
          Knowledge
        </h1>
        <p className="text-sm text-muted-foreground">
          What this project&apos;s agents have learned and saved. Correct a note or delete one that
          is wrong.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <form onSubmit={applySearch} className="flex items-end gap-2">
          <div className="space-y-1.5">
            <Label htmlFor="knowledge-search">Search notes</Label>
            <Input
              id="knowledge-search"
              type="search"
              className="w-64"
              placeholder="Search by meaning or words"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
              }}
            />
          </div>
          <Button type="submit" variant="secondary">
            <Search className="size-4" />
            Search
          </Button>
        </form>
        <div className="space-y-1.5">
          <Label htmlFor="knowledge-type">Type</Label>
          <Select
            value={toSelectValue(type)}
            onValueChange={(value) => {
              const picked = fromSelectValue(value);
              setType(NOTE_TYPES.find((t) => t === picked));
              setLimit(NOTES_PAGE_SIZE);
            }}
          >
            <SelectTrigger id="knowledge-type" className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_NONE}>All types</SelectItem>
              {NOTE_TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {NOTE_TYPE_LABELS[t]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {q !== "" && query.data && !query.data.used_vectors && query.data.detail && (
        <p className="text-xs text-muted-foreground">{query.data.detail}</p>
      )}

      {query.isLoading && (
        <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading notes...
        </div>
      )}

      {query.error && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading notes</AlertTitle>
          <AlertDescription className="space-y-2">
            <p>{errorText(query.error)}</p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void query.refetch();
              }}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {!query.isLoading && !query.error && notes.length === 0 && (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-sm text-muted-foreground">
            {filtered ? "No notes match your search or filter." : "No notes yet."}
          </p>
        </div>
      )}

      {notes.length > 0 && (
        <ul className="space-y-3">
          {notes.map((note) => (
            <NoteRow key={note.id} projectId={projectId} note={note} />
          ))}
        </ul>
      )}

      {(notes.length >= limit || query.isPlaceholderData) && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            disabled={query.isPlaceholderData}
            onClick={() => {
              setLimit(limit + NOTES_PAGE_SIZE);
            }}
          >
            {query.isPlaceholderData && <Loader2 className="size-4 animate-spin" />}
            Show more
          </Button>
        </div>
      )}
    </div>
  );
}
