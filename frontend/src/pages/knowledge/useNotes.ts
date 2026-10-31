import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import type { NoteType } from "./noteTypes";

// One page and one "Show more" step. The server clamps what it serves; this only sizes the asks.
export const NOTES_PAGE_SIZE = 20;

export type NoteFilters = { q: string; type: NoteType | undefined; limit: number };

export function notesQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "notes"] as const;
}

export function useNotes(projectId: string | undefined, filters: NoteFilters) {
  useInvalidateOnEvents(projectId, notesQueryKey(projectId), ["note.saved", "note.deleted"]);

  return useQuery({
    queryKey: [...notesQueryKey(projectId), filters],
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/notes", {
        params: {
          path: { project_id: projectId },
          query: {
            q: filters.q === "" ? undefined : filters.q,
            type: filters.type,
            limit: filters.limit,
          },
        },
      });
      return unwrap(res);
    },
    // Keeps the rows on screen while a larger page loads.
    placeholderData: keepPreviousData,
    enabled: Boolean(projectId),
  });
}
