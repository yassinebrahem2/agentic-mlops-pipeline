import type { QueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";

export type Hook = components["schemas"]["HookView"];

export const hooksKey = (projectId: string) => ["projects", projectId, "hooks"] as const;

/** A route that returns the changed hook updates its row without refetching the list. */
export function replaceHook(queryClient: QueryClient, projectId: string, saved: Hook): void {
  queryClient.setQueryData<Hook[]>(hooksKey(projectId), (old) =>
    old?.map((hook) => (hook.id === saved.id ? saved : hook)),
  );
}
