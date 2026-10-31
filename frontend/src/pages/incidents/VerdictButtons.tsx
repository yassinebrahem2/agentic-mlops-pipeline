import { useState } from "react";
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
import { ActionError } from "./ActionError";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  VERDICT_LABELS,
  type ResultCategory,
  type VerdictDraft,
} from "./labels";
import { useRecordVerdict } from "./useIncidentActions";

type Props = { projectId: string; incidentId: string };

function trimmedOrUndefined(text: string): string | undefined {
  const trimmed = text.trim();
  return trimmed === "" ? undefined : trimmed;
}

export function VerdictButtons({ projectId, incidentId }: Props) {
  const mutation = useRecordVerdict({ projectId, incidentId });
  const [note, setNote] = useState("");
  const [choosingType, setChoosingType] = useState(false);
  const [category, setCategory] = useState<ResultCategory | undefined>(undefined);
  const [subtype, setSubtype] = useState("");
  const idPrefix = `verdict-${incidentId}`;

  const send = (draft: VerdictDraft) => {
    const noteText = trimmedOrUndefined(note);
    mutation.mutate(noteText ? { ...draft, note: noteText } : draft, {
      onSuccess: () => {
        setNote("");
        setChoosingType(false);
        setCategory(undefined);
        setSubtype("");
      },
    });
  };

  const sendWrongType = () => {
    if (!category) return;
    const subtypeText = trimmedOrUndefined(subtype);
    send({
      value: "wrong_type",
      right_category: category,
      ...(subtypeText ? { right_subtype: subtypeText } : {}),
    });
  };

  const busy = mutation.isPending;

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-note`}>Note (optional)</Label>
        <Input
          id={`${idPrefix}-note`}
          value={note}
          disabled={busy}
          onChange={(e) => {
            setNote(e.target.value);
          }}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => {
            send({ value: "real_issue" });
          }}
        >
          {VERDICT_LABELS.real_issue}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => {
            send({ value: "not_an_issue" });
          }}
        >
          {VERDICT_LABELS.not_an_issue}
        </Button>
        <Button
          variant={choosingType ? "secondary" : "outline"}
          size="sm"
          disabled={busy}
          aria-expanded={choosingType}
          onClick={() => {
            setChoosingType((open) => !open);
          }}
        >
          {VERDICT_LABELS.wrong_type}
        </Button>
      </div>
      {choosingType && (
        <div className="flex flex-wrap items-end gap-3 rounded-md border p-3">
          <div className="space-y-1.5">
            <Label htmlFor={`${idPrefix}-category`}>Right category</Label>
            <Select
              value={category ?? ""}
              onValueChange={(value) => {
                setCategory(CATEGORIES.find((c) => c === value));
              }}
            >
              <SelectTrigger id={`${idPrefix}-category`} className="w-52">
                <SelectValue placeholder="Choose a category" />
              </SelectTrigger>
              <SelectContent>
                {CATEGORIES.map((c) => (
                  <SelectItem key={c} value={c}>
                    {CATEGORY_LABELS[c]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${idPrefix}-subtype`}>Right subtype (optional)</Label>
            <Input
              id={`${idPrefix}-subtype`}
              className="w-48"
              value={subtype}
              disabled={busy}
              onChange={(e) => {
                setSubtype(e.target.value);
              }}
            />
          </div>
          <Button size="sm" disabled={busy || !category} onClick={sendWrongType}>
            Record wrong type
          </Button>
        </div>
      )}
      <ActionError error={mutation.error} />
    </div>
  );
}
