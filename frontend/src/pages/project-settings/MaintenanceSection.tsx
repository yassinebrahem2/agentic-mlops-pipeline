import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, unwrap } from "@/lib/api";
import { ActionError } from "@/pages/incidents/ActionError";
import { expectationsKey } from "./expectationsKey";
import { formatTime } from "@/lib/format";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";

type MaintenanceWindow = components["schemas"]["MaintenanceWindowView"];

const windowsKey = (projectId: string) => ["projects", projectId, "maintenance-windows"] as const;

// A window changes which expectations are suspended, so the list under "Checks and schedules" follows it.
async function refreshAfterChange(queryClient: QueryClient, projectId: string) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: windowsKey(projectId) }),
    queryClient.invalidateQueries({ queryKey: expectationsKey(projectId) }),
  ]);
}

// The routes have no event of their own: the list refreshes when this page changes it and when an
// approved proposal (which can add a window) lands.
export function MaintenanceSection({ projectId }: { projectId: string }) {
  const params = { path: { project_id: projectId } };
  const windows = useQuery({
    queryKey: windowsKey(projectId),
    queryFn: async () =>
      unwrap(await api.GET("/api/projects/{project_id}/maintenance-windows", { params })),
  });
  useInvalidateOnEvents(projectId, windowsKey(projectId), ["proposal.decided", "handoff.approved"]);
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="maintenance-title">
      <h2 id="maintenance-title" className="text-lg font-semibold">
        Maintenance windows
      </h2>
      <p className="text-sm text-muted-foreground">
        While a window is open, the checks and schedules in its scope are not expected to report.
      </p>
      <QueryStatus query={windows} loading="Loading maintenance windows..." />
      {windows.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">No maintenance windows.</p>
      )}
      {windows.data && windows.data.length > 0 && (
        <ul className="space-y-3">
          {windows.data.map((window) => (
            <WindowRow key={window.id} projectId={projectId} window={window} />
          ))}
        </ul>
      )}
      <WindowForm projectId={projectId} />
    </section>
  );
}

function WindowRow({ projectId, window }: { projectId: string; window: MaintenanceWindow }) {
  return (
    <li aria-label={window.reason} className="space-y-2 rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{window.reason}</span>
        <span className="text-muted-foreground">
          {formatTime(window.start)} to {formatTime(window.end)}
        </span>
        <span className="ml-auto">
          <DeleteWindowDialog projectId={projectId} window={window} />
        </span>
      </div>
      <ul className="flex flex-wrap gap-1" aria-label="Names covered">
        {window.scope.map((entry) => (
          <li key={entry}>
            <Badge variant="outline">{entry}</Badge>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        {window.proposal_id
          ? `Proposed by an agent (proposal ${window.proposal_id})`
          : `Created by ${window.created_by}`}
      </p>
    </li>
  );
}

function DeleteWindowDialog({
  projectId,
  window,
}: {
  projectId: string;
  window: MaintenanceWindow;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const remove = useMutation({
    mutationFn: async () => {
      unwrap(
        await api.DELETE("/api/projects/{project_id}/maintenance-windows/{window_id}", {
          params: { path: { project_id: projectId, window_id: window.id } },
        }),
      );
    },
    onSuccess: async () => {
      setOpen(false);
      await refreshAfterChange(queryClient, projectId);
    },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        remove.reset();
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button
          variant="destructive"
          size="sm"
          aria-label={`Delete window ${window.scope.join(", ") || window.id}`}
        >
          Delete
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete this maintenance window?</DialogTitle>
          <DialogDescription>
            &ldquo;{window.reason}&rdquo; is removed, so its checks and schedules are expected to
            report again. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <ActionError error={remove.error} />
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            disabled={remove.isPending}
            onClick={() => {
              remove.mutate();
            }}
          >
            Delete window
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function WindowForm({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [scope, setScope] = useState("");
  const [reason, setReason] = useState("");
  const create = useMutation({
    mutationFn: async (body: components["schemas"]["WindowSpec"]) =>
      unwrap(
        await api.POST("/api/projects/{project_id}/maintenance-windows", {
          params: { path: { project_id: projectId } },
          body,
        }),
      ),
    onSuccess: async () => {
      setStart("");
      setEnd("");
      setScope("");
      setReason("");
      await refreshAfterChange(queryClient, projectId);
    },
  });
  const startAt = new Date(start);
  const endAt = new Date(end);
  const ready =
    !Number.isNaN(startAt.getTime()) && !Number.isNaN(endAt.getTime()) && reason.trim() !== "";

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready) return;
    create.mutate({
      start: startAt.toISOString(),
      end: endAt.toISOString(),
      // Sent as typed: the server decides which names it accepts.
      scope: scope
        .split(",")
        .map((entry) => entry.trim())
        .filter((entry) => entry !== ""),
      reason: reason.trim(),
    });
  };

  return (
    <form className="space-y-3 border-t pt-4" onSubmit={submit}>
      <h3 className="font-medium">Add a maintenance window</h3>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="window-start">Starts</Label>
          <Input
            id="window-start"
            type="datetime-local"
            value={start}
            disabled={create.isPending}
            onChange={(event) => {
              setStart(event.target.value);
            }}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="window-end">Ends</Label>
          <Input
            id="window-end"
            type="datetime-local"
            value={end}
            disabled={create.isPending}
            onChange={(event) => {
              setEnd(event.target.value);
            }}
          />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="window-scope">Scope</Label>
        <Input
          id="window-scope"
          value={scope}
          disabled={create.isPending}
          aria-describedby="window-scope-help"
          onChange={(event) => {
            setScope(event.target.value);
          }}
        />
        <p id="window-scope-help" className="text-sm text-muted-foreground">
          The names of the checks and schedules it covers, separated by commas.
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="window-reason">Reason</Label>
        <Input
          id="window-reason"
          value={reason}
          disabled={create.isPending}
          onChange={(event) => {
            setReason(event.target.value);
          }}
        />
      </div>
      <ActionError error={create.error} />
      <Button type="submit" disabled={!ready || create.isPending}>
        {create.isPending ? "Adding..." : "Add window"}
      </Button>
    </form>
  );
}
