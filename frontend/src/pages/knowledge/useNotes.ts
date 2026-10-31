import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import type { NoteType } from "./noteTypes";

export type NoteFilters = { q: string; type: NoteType | undefined };

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
          query: { q: filters.q === "" ? undefined : filters.q, type: filters.type },
        },
      });
      return unwrap(res);
    },
    enabled: Boolean(projectId),
  });
}
