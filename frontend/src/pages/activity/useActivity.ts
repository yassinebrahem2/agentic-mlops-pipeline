import { useInfiniteQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import { PROJECT_EVENT_TYPES, type ProjectEventType } from "@/lib/useProjectEvents";

export type ActivityEntry = components["schemas"]["ActivityPage"]["entries"][number];

export type ActivityFilters = { type: ProjectEventType | undefined; source: string | undefined };

export function activityQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "activity"] as const;
}

export function useActivity(projectId: string | undefined, filters: ActivityFilters) {
  // Activity is the trail of everything, so any project event makes it stale.
  useInvalidateOnEvents(projectId, activityQueryKey(projectId), PROJECT_EVENT_TYPES);

  return useInfiniteQuery({
    queryKey: [...activityQueryKey(projectId), filters],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      if (!projectId) throw new Error("No projectId");
      const res = await api.GET("/api/projects/{project_id}/activity", {
        params: {
          path: { project_id: projectId },
          query: { type: filters.type, source: filters.source, before: pageParam },
        },
      });
      return unwrap(res);
    },
    getNextPageParam: (last) => last.next_before ?? undefined,
    enabled: Boolean(projectId),
  });
}

/** One identity per entry: the dedupe in flattenPages, the React key and the test id use it. */
export function entryIdentity(entry: ActivityEntry): string {
  return `${entry.kind}-${entry.kind === "event" ? entry.event.id : entry.audit.id}`;
}

/**
 * Flattens the loaded pages. A refetch after a new record shifts older entries down a page,
 * so the same entry can appear in two pages: the first occurrence wins.
 */
export function flattenPages(pages: readonly { entries: ActivityEntry[] }[]): ActivityEntry[] {
  const seen = new Set<string>();
  const flat: ActivityEntry[] = [];
  for (const page of pages) {
    for (const entry of page.entries) {
      const key = entryIdentity(entry);
      if (!seen.has(key)) {
        seen.add(key);
        flat.push(entry);
      }
    }
  }
  return flat;
}
