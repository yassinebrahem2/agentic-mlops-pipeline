import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, errorText, unwrap } from "@/lib/api";

type Level = components["schemas"]["PermissionsSettings"]["level"];

// The one place the level names and their explanations live; the Record keeps it complete.
const LEVEL_COPY: Record<Level, { label: string; help: string }> = {
  suggest: {
    label: "Suggest",
    help: "Agents read and message freely and propose every code change as a diff in their answer; they run and deploy nothing, and ask before they use GitHub.",
  },
  ask: {
    label: "Ask",
    help: "Agents read and message freely and ask before they change code, run commands, use GitHub or deploy.",
  },
  autonomous: {
    label: "Autonomous",
    help: "Agents change code, run commands and use GitHub without asking; they still ask before they deploy.",
  },
  bypass: {
    label: "Bypass",
    help: "Agents do everything without asking, deploys included.",
  },
};
const LEVELS: Level[] = ["suggest", "ask", "autonomous", "bypass"];

const permissionsKey = (projectId: string) =>
  ["projects", projectId, "settings", "permissions"] as const;

function isLevel(value: string): value is Level {
  return LEVELS.some((level) => level === value);
}

export function PermissionsSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const params = { path: { project_id: projectId } };
  const [confirmingBypass, setConfirmingBypass] = useState(false);
  const permissions = useQuery({
    queryKey: permissionsKey(projectId),
    queryFn: async () =>
      unwrap(await api.GET("/api/projects/{project_id}/settings/permissions", { params })),
  });
  const save = useMutation({
    mutationFn: async (level: Level) =>
      unwrap(
        await api.PUT("/api/projects/{project_id}/settings/permissions", {
          params,
          body: { level },
        }),
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData(permissionsKey(projectId), saved);
    },
    onSettled: () => {
      setConfirmingBypass(false);
    },
  });
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="permissions-title">
      <h2 id="permissions-title" className="text-lg font-semibold">
        Permissions
      </h2>
      {permissions.isPending && <p role="status">Loading permissions...</p>}
      {permissions.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(permissions.error)}
        </p>
      )}
      {permissions.data && (
        <>
          <div className="space-y-2">
            <Label htmlFor="permission-level">Permission level</Label>
            <Select
              value={permissions.data.level}
              disabled={save.isPending}
              onValueChange={(value) => {
                if (!isLevel(value)) return;
                if (value === "bypass") setConfirmingBypass(true);
                else save.mutate(value);
              }}
            >
              <SelectTrigger id="permission-level" className="w-full sm:w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {LEVELS.map((level) => (
                  <SelectItem key={level} value={level}>
                    {LEVEL_COPY[level].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <ul aria-label="What each level allows" className="space-y-1 text-sm">
            {LEVELS.map((level) => (
              <li key={level}>
                <strong>{LEVEL_COPY[level].label}</strong>: {LEVEL_COPY[level].help}
              </li>
            ))}
          </ul>
          <p className="text-sm text-muted-foreground">
            A change applies from the agents' next tool call.
          </p>
        </>
      )}
      {save.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(save.error)}
        </p>
      )}
      <Dialog
        open={confirmingBypass}
        onOpenChange={(open) => {
          if (!save.isPending) setConfirmingBypass(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Allow bypass for this project?</DialogTitle>
            <DialogDescription>
              Agents will then act without asking. The sandbox, the worktree and the audit log still
              apply.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={save.isPending}
              onClick={() => {
                setConfirmingBypass(false);
              }}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={save.isPending}
              onClick={() => {
                save.mutate("bypass");
              }}
            >
              {save.isPending ? "Saving..." : "Allow bypass"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
