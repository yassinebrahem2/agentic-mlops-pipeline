import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type SyntheticEvent } from "react";
import type { components } from "@/api/schema";
import { ConnectionFindings } from "@/components/ConnectionFindings";
import { FolderBrowser } from "@/components/FolderBrowser";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";
import { useAppInfo } from "@/lib/useAppInfo";

type ConnectionView = components["schemas"]["ConnectionView"];

export interface ConnectionDialogProps {
  projectId: string;
  connection?: ConnectionView | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ConnectionDialog({
  projectId,
  connection,
  open,
  onOpenChange,
}: ConnectionDialogProps) {
  const queryClient = useQueryClient();
  const appInfo = useAppInfo();
  const isServerMode = appInfo.data?.mode === "server";
  const isEditing = Boolean(connection);

  const [name, setName] = useState("");
  const [chosenPath, setChosenPath] = useState("");
  const [inspectedPath, setInspectedPath] = useState<string | null>(null);

  const inspect = useMutation({
    mutationFn: async (path: string) =>
      unwrap(
        await api.POST("/api/projects/{project_id}/connections/inspect", {
          params: { path: { project_id: projectId } },
          body: { kind: "local_folder", settings: { path } },
        }),
      ),
    onSuccess: (_data, path) => {
      setInspectedPath(path);
    },
  });

  const create = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/projects/{project_id}/connections", {
          params: { path: { project_id: projectId } },
          body: {
            kind: "local_folder",
            name: name.trim(),
            settings: { path: chosenPath },
          },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["projects", projectId, "connections"],
      });
      onOpenChange(false);
    },
  });

  const update = useMutation({
    mutationFn: async () => {
      if (!connection) {
        throw new Error("No connection to update");
      }
      return unwrap(
        await api.PATCH("/api/projects/{project_id}/connections/{connection_id}", {
          params: { path: { project_id: projectId, connection_id: connection.id } },
          body: {
            settings: { path: chosenPath },
          },
        }),
      );
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["projects", projectId, "connections"],
      });
      onOpenChange(false);
    },
  });

  useEffect(() => {
    if (open) {
      setName(connection?.name ?? "");
      setChosenPath(
        connection?.settings && typeof connection.settings.path === "string"
          ? connection.settings.path
          : "",
      );
      setInspectedPath(null);
      inspect.reset();
      create.reset();
      update.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, connection]);

  const handleChooseFolder = (path: string) => {
    setChosenPath(path);
    setInspectedPath(null);
    create.reset();
    update.reset();
    inspect.mutate(path);
  };

  const isSubmitting = create.isPending || update.isPending;
  const inspectSuccess = inspect.isSuccess && inspectedPath === chosenPath && chosenPath !== "";
  const nameValid = isEditing ? true : name.trim().length > 0;
  const canSubmit = inspectSuccess && nameValid && !isSubmitting && !inspect.isPending;

  const handleSubmit = (e: SyntheticEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    if (isEditing) {
      update.mutate();
    } else {
      create.mutate();
    }
  };

  const initialPath =
    connection?.settings && typeof connection.settings.path === "string"
      ? connection.settings.path
      : undefined;

  const connectionName = connection?.name ?? "";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEditing ? "Edit connection" : "Add connection"}</DialogTitle>
          <DialogDescription>
            {isEditing
              ? `Change the folder for connection ${connectionName}.`
              : "Connect a local data folder as a read-only data source."}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="connection-name">Name</Label>
            <Input
              id="connection-name"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
              }}
              disabled={isEditing}
              readOnly={isEditing}
              placeholder="e.g. sales-data"
            />
          </div>

          <div className="space-y-2">
            <Label>Folder</Label>
            {isServerMode && (
              <p className="text-sm text-muted-foreground">This is a folder on the server.</p>
            )}
            <FolderBrowser
              initialPath={initialPath}
              onChoose={handleChooseFolder}
              isSubmitting={inspect.isPending}
              busyLabel="Inspecting..."
            />
          </div>

          {chosenPath && (
            <p className="text-sm text-muted-foreground">
              Selected folder:{" "}
              <span className="font-mono font-medium text-foreground">{chosenPath}</span>
            </p>
          )}

          {inspect.error && (
            <div role="alert" className="rounded-md bg-destructive/15 p-3 text-sm text-destructive">
              {errorText(inspect.error)}
            </div>
          )}

          {inspect.data && <ConnectionFindings findings={inspect.data} />}

          {(create.error || update.error) && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(create.error ?? update.error)}
            </p>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={isSubmitting}
              onClick={() => {
                onOpenChange(false);
              }}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit}>
              {isSubmitting
                ? isEditing
                  ? "Saving..."
                  : "Creating..."
                : isEditing
                  ? "Save"
                  : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
