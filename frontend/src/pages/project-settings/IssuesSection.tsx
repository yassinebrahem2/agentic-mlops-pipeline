import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, errorText, unwrap } from "@/lib/api";

type Priority = components["schemas"]["IssuesSettings"]["min_priority"];

const PRIORITY_LABELS: Record<Priority, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  critical: "Critical",
};
const PRIORITIES: Priority[] = ["low", "medium", "high", "critical"];

const issuesKey = (projectId: string) => ["projects", projectId, "settings", "issues"] as const;

function isPriority(value: string): value is Priority {
  return PRIORITIES.some((priority) => priority === value);
}

export function IssuesSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const params = { path: { project_id: projectId } };
  const issues = useQuery({
    queryKey: issuesKey(projectId),
    queryFn: async () =>
      unwrap(await api.GET("/api/projects/{project_id}/settings/issues", { params })),
  });
  const save = useMutation({
    mutationFn: async (min_priority: Priority) =>
      unwrap(
        await api.PUT("/api/projects/{project_id}/settings/issues", {
          params,
          body: { min_priority },
        }),
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData(issuesKey(projectId), saved);
    },
  });
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="issues-title">
      <h2 id="issues-title" className="text-lg font-semibold">
        GitHub issues
      </h2>
      {issues.isPending && <p role="status">Loading issue settings...</p>}
      {issues.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(issues.error)}
        </p>
      )}
      {issues.data && (
        <div className="space-y-2">
          <Label htmlFor="issue-min-priority">Minimum priority</Label>
          <Select
            value={issues.data.min_priority}
            disabled={save.isPending}
            onValueChange={(value) => {
              if (isPriority(value)) save.mutate(value);
            }}
          >
            <SelectTrigger id="issue-min-priority" className="w-full sm:w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PRIORITIES.map((priority) => (
                <SelectItem key={priority} value={priority}>
                  {PRIORITY_LABELS[priority]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-sm text-muted-foreground">
            Agents do not open GitHub issues below this priority.
          </p>
        </div>
      )}
      {save.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(save.error)}
        </p>
      )}
    </section>
  );
}
