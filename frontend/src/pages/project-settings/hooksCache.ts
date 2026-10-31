import type { QueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";

export type Hook = components["schemas"]["HookView"];

export const hooksKey = (projectId: string) => ["projects", projectId, "hooks"] as const;

/** A route that returns the changed hook updates its row without refetching the list. */
export function replaceHook(queryClient: QueryClient, projectId: string, saved: Hook): void {
  queryClient.setQueryData<Hook[]>(hooksKey(projectId), (old) =>
    old?.map((hook) => (hook.id === saved.id ? saved : hook)),
  );
}

/** One source for the hooks list query, shared by the hooks section and the rules that name a hook. */
export const hooksQuery = (projectId: string) => ({
  queryKey: hooksKey(projectId),
  queryFn: async () =>
    unwrap(
      await api.GET("/api/projects/{project_id}/hooks", {
        params: { path: { project_id: projectId } },
      }),
    ),
});

// Exhaustive over the generated union, so a kind the backend adds fails to compile here.
export const KIND_LABELS: Record<Hook["spec"]["kind"], string> = {
  train: "Train",
  evaluate: "Evaluate",
  deploy: "Deploy",
  rollback: "Roll back",
  rerun: "Re-run",
  hold: "Hold",
  fall_back: "Fall back",
};
