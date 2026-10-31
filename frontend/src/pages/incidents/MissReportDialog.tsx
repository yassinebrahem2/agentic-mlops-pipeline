import { useState, type SubmitEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
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
import { fromSelectValue, SELECT_NONE, toSelectValue } from "@/lib/selectNone";
import { ActionError } from "./ActionError";
import { CATEGORIES, CATEGORY_LABELS, type ResultCategory } from "./labels";
import { useReportMiss } from "./useIncidentActions";

export function MissReportDialog({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState<ResultCategory | undefined>(undefined);
  const [occurredAt, setOccurredAt] = useState("");
  const mutation = useReportMiss(projectId);

  const when = new Date(occurredAt);
  const ready = description.trim() !== "" && !Number.isNaN(when.getTime());

  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    if (!ready) return;
    mutation.mutate(
      { description: description.trim(), category, occurredAt: when },
      {
        onSuccess: () => {
          setOpen(false);
          setDescription("");
          setCategory(undefined);
          setOccurredAt("");
        },
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) mutation.reset();
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          Report a miss
        </Button>
      </DialogTrigger>
      <DialogContent>
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>Report a miss</DialogTitle>
            <DialogDescription>
              Tell the monitor about a problem it did not catch, so its checks can be improved.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="miss-description">What happened</Label>
            <Textarea
              id="miss-description"
              value={description}
              onChange={(e) => {
                setDescription(e.target.value);
              }}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="miss-category">Category (optional)</Label>
            <Select
              value={toSelectValue(category)}
              onValueChange={(value) => {
                const picked = fromSelectValue(value);
                setCategory(CATEGORIES.find((c) => c === picked));
              }}
            >
              <SelectTrigger id="miss-category" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_NONE}>Not sure</SelectItem>
                {CATEGORIES.map((c) => (
                  <SelectItem key={c} value={c}>
                    {CATEGORY_LABELS[c]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="miss-when">When it happened</Label>
            <Input
              id="miss-when"
              type="datetime-local"
              value={occurredAt}
              onChange={(e) => {
                setOccurredAt(e.target.value);
              }}
            />
          </div>
          <ActionError error={mutation.error} />
          <DialogFooter>
            <Button type="submit" disabled={!ready || mutation.isPending}>
              Send report
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
