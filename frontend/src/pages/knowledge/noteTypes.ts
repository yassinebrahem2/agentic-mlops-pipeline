import type { components } from "@/api/schema";

export type NoteView = components["schemas"]["NoteView"];
export type NoteType = NoteView["type"];

// The Record forces a label for every type the API knows; the keys are the only runtime list.
export const NOTE_TYPE_LABELS: Record<NoteType, string> = {
  fact: "Fact",
  decision: "Decision",
  incident: "Incident",
  runbook: "Runbook",
  doc: "Doc",
};

export const NOTE_TYPES = Object.keys(NOTE_TYPE_LABELS) as NoteType[];

type NoteUpdate = components["schemas"]["NoteUpdate"];

/** Only what differs from the saved note: an unchanged field is not sent. */
export function changedFields(
  note: NoteView,
  draft: { type: NoteType; title: string; text: string },
): NoteUpdate {
  const patch: NoteUpdate = {};
  if (draft.type !== note.type) patch.type = draft.type;
  if (draft.title !== note.title) patch.title = draft.title;
  if (draft.text !== note.text) patch.text = draft.text;
  return patch;
}
