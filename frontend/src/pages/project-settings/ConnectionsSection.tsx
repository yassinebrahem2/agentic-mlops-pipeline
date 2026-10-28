import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { components } from "@/api/schema";
import { ConnectionFindings } from "@/components/ConnectionFindings";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, errorText, unwrap } from "@/lib/api";
import { useProjectEvents } from "@/lib/useProjectEvents";
import { ConnectionDialog } from "./ConnectionDialog";

type ConnectionView = components["schemas"]["ConnectionView"];

const connectionsKey = (projectId: string) => ["projects", projectId, "connections"] as const;

export function ConnectionsSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [editingConnection, setEditingConnection] = useState<ConnectionView | null>(null);
  const [removingConnection, setRemovingConnection] = useState<ConnectionView | null>(null);

  const connections = useQuery({
    queryKey: connectionsKey(projectId),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/connections", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: connectionsKey(projectId) });
  };

  useProjectEvents(projectId, {
    onEvent: (event) => {
      if (event.type.startsWith("connection.")) refresh();
    },
    onResync: refresh,
  });

  const remove = useMutation({
    mutationFn: async (connectionId: string) => {
      unwrap(
        await api.DELETE("/api/projects/{project_id}/connections/{connection_id}", {
          params: { path: { project_id: projectId, connection_id: connectionId } },
        }),
      );
    },
    onSuccess: () => {
      setRemovingConnection(null);
      refresh();
    },
  });

  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="connections-title">
      <div className="flex items-center justify-between">
        <div>
          <h2 id="connections-title" className="text-lg font-semibold">
            Connections
          </h2>
          <p className="text-sm text-muted-foreground">
            Local data folders mounted as read-only data sources.
          </p>
        </div>
        <Button
          onClick={() => {
            setAddOpen(true);
          }}
        >
          Add connection
        </Button>
      </div>

      {connections.isPending && <p role="status">Loading connections...</p>}
      {connections.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(connections.error)}
        </p>
      )}

      {connections.data && connections.data.length === 0 && (
        <p className="text-sm text-muted-foreground">No connections yet.</p>
      )}

      {connections.data && connections.data.length > 0 && (
        <div className="space-y-4">
          {connections.data.map((connection) => (
            <ConnectionRow
              key={connection.id}
              projectId={projectId}
              connection={connection}
              onEdit={(c) => {
                setEditingConnection(c);
              }}
              onRemove={(c) => {
                setRemovingConnection(c);
              }}
            />
          ))}
        </div>
      )}

      <ConnectionDialog
        projectId={projectId}
        connection={editingConnection}
        open={addOpen || Boolean(editingConnection)}
        onOpenChange={(next) => {
          if (!next) {
            setAddOpen(false);
            setEditingConnection(null);
          }
        }}
      />

      <Dialog
        open={Boolean(removingConnection)}
        onOpenChange={(next) => {
          if (!remove.isPending && !next) setRemovingConnection(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove {removingConnection?.name}?</DialogTitle>
            <DialogDescription>
              The connection will be removed from this project. The folder on disk is not touched.
            </DialogDescription>
          </DialogHeader>
          {remove.error && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(remove.error)}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={remove.isPending}
              onClick={() => {
                setRemovingConnection(null);
              }}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => {
                if (removingConnection) {
                  remove.mutate(removingConnection.id);
                }
              }}
            >
              {remove.isPending ? "Removing..." : "Remove"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function ConnectionRow({
  projectId,
  connection,
  onEdit,
  onRemove,
}: {
  projectId: string;
  connection: ConnectionView;
  onEdit: (connection: ConnectionView) => void;
  onRemove: (connection: ConnectionView) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const tables = useQuery({
    queryKey: ["projects", projectId, "connections", connection.id, "tables"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/connections/{connection_id}/tables", {
          params: { path: { project_id: projectId, connection_id: connection.id } },
        }),
      ),
    enabled: connection.status === "ok",
  });

  return (
    <div className="space-y-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-base font-semibold">{connection.name}</span>
          <Badge variant="outline">{connection.kind}</Badge>
          {connection.status === "ok" ? (
            <Badge variant="secondary">ok</Badge>
          ) : (
            <Badge variant="destructive">unreachable</Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              onEdit(connection);
            }}
          >
            Edit
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="text-destructive hover:bg-destructive/10"
            onClick={() => {
              onRemove(connection);
            }}
          >
            Remove
          </Button>
        </div>
      </div>

      {connection.status === "unreachable" && connection.status_detail && (
        <p className="text-sm text-destructive">{connection.status_detail}</p>
      )}

      <dl className="grid gap-1 text-sm sm:grid-cols-[8rem_1fr]">
        <dt className="text-muted-foreground">Data version</dt>
        <dd className="font-mono">{connection.data_version ?? "—"}</dd>

        <dt className="text-muted-foreground">Tables</dt>
        <dd className="flex items-center gap-2">
          {connection.status !== "ok" ? (
            <span>—</span>
          ) : tables.isPending ? (
            <span>Loading tables...</span>
          ) : tables.error ? (
            <span className="text-destructive">{errorText(tables.error)}</span>
          ) : (
            <>
              <span>
                {tables.data.length} {tables.data.length === 1 ? "table" : "tables"}
              </span>
              {tables.data.length > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-6 px-2 text-xs"
                  onClick={() => {
                    setExpanded(!expanded);
                  }}
                  aria-expanded={expanded}
                >
                  {expanded ? "Hide tables" : "Show tables"}
                </Button>
              )}
            </>
          )}
        </dd>
      </dl>

      {expanded && tables.data && tables.data.length > 0 && (
        <div className="space-y-1 rounded-md border bg-muted/20 p-2">
          <ul className="space-y-1">
            {tables.data.map((table) => (
              <li key={table.name} className="flex items-center justify-between px-2 py-1 text-sm">
                <span className="font-mono font-medium">{table.name}</span>
                <Badge variant="outline" className="text-xs uppercase">
                  {table.format}
                </Badge>
              </li>
            ))}
          </ul>
        </div>
      )}

      <ConnectionFindings findings={connection.findings} />
    </div>
  );
}
