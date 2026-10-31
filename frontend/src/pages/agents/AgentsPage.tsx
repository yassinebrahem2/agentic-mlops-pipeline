import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertCircle, AlertTriangle } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router";
import { ProjectStatus } from "@/components/ProjectStatus";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { useAgentStatus } from "@/lib/useAgentStatus";
import { useProject } from "@/lib/useProject";
import { useProjectAgents } from "@/lib/useProjectAgents";
import { useProjectEvents } from "@/lib/useProjectEvents";
import { useProjectPermissions } from "@/lib/useProjectPermissions";
import { AgentList, AgentSelect } from "./AgentList";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { TaskBar } from "./TaskBar";
import { useSession } from "./session/useSession";

export function AgentsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  // Every query, the session and its stream need a project; without one there is nothing to show.
  if (!projectId) return null;
  return <Agents projectId={projectId} />;
}

function Agents({ projectId }: { projectId: string }) {
  const [searchParams, setSearchParams] = useSearchParams();

  const projectQuery = useProject(projectId);
  const agentsQuery = useProjectAgents(projectId);
  const statusQuery = useAgentStatus(projectId);
  const permissionsQuery = useProjectPermissions(projectId);

  const chatAgents = (agentsQuery.data ?? []).filter((agent) => agent.chat);
  const agentParam = searchParams.get("agent");
  const selectedAgent = chatAgents.find((agent) => agent.name === agentParam) ?? chatAgents[0];

  const selectedAgentName = selectedAgent?.name ?? "";
  const selectedAgentDisplayName = selectedAgent?.display_name ?? selectedAgentName;

  const session = useSession(projectId, selectedAgentName);
  const currentTask = session.state?.current_task;

  const [clearDialogOpen, setClearDialogOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const compactMutation = useMutation({
    mutationFn: async () => {
      const res = await api.POST("/api/projects/{project_id}/agents/{agent}/session/compact", {
        params: { path: { project_id: projectId, agent: selectedAgentName } },
      });
      unwrap(res);
    },
    onSuccess: () => {
      setActionError(null);
      void session.reload();
    },
    onError: (err) => {
      setActionError(errorText(err));
    },
  });

  const clearMutation = useMutation({
    mutationFn: async () => {
      const res = await api.POST("/api/projects/{project_id}/agents/{agent}/session/clear", {
        params: { path: { project_id: projectId, agent: selectedAgentName } },
      });
      unwrap(res);
    },
    onSuccess: () => {
      setClearDialogOpen(false);
      setActionError(null);
      void session.reload();
      void clearedEventsQuery.refetch();
    },
    onError: (err) => {
      setClearDialogOpen(false);
      setActionError(errorText(err));
    },
  });

  const clearedEventsQuery = useQuery({
    queryKey: ["projects", projectId, "cleared-events", selectedAgentName],
    queryFn: async () => {
      const res = await api.GET("/api/projects/{project_id}/activity", {
        params: {
          path: { project_id: projectId },
          query: { type: "session.cleared", limit: 200 },
        },
      });
      if (res.error) return [];
      return res.data.entries
        .filter((e): e is Extract<typeof e, { kind: "event" }> => e.kind === "event")
        .map((e) => e.event)
        .filter(
          (ev) =>
            typeof ev.payload === "object" &&
            (ev.payload as Record<string, unknown>).agent === selectedAgentName,
        )
        .map((ev) => {
          const payload = ev.payload as { person?: string; last_message_id?: string | null };
          return {
            person: payload.person || "user",
            last_message_id: payload.last_message_id ?? null,
          };
        });
    },
    enabled: Boolean(projectId && selectedAgentName),
  });

  useProjectEvents(projectId, {
    onEvent: (event) => {
      if (event.type === "session.cleared") {
        void clearedEventsQuery.refetch();
      }
    },
  });

  if (agentsQuery.isLoading) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-muted-foreground">
        Loading agents...
      </div>
    );
  }

  if (agentsQuery.isError) {
    return (
      <div className="p-8">
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Failed to load agents</AlertTitle>
          <AlertDescription className="mt-2 flex flex-col items-start gap-2">
            <span>{errorText(agentsQuery.error)}</span>
            <Button variant="outline" size="sm" onClick={() => void agentsQuery.refetch()}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  if (chatAgents.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-muted-foreground">
        No chat agents found.
      </div>
    );
  }

  const project = projectQuery.data;
  const showProjectStatus = project && project.status !== "ok";

  return (
    <div className="flex h-full overflow-hidden">
      <AgentList
        className="hidden md:flex"
        agents={statusQuery.data ?? []}
        isLoading={statusQuery.isLoading}
        error={statusQuery.error}
        selectedAgent={selectedAgentName}
        onSelectAgent={(agentName) => {
          setSearchParams((prev) => {
            const next = new URLSearchParams(prev);
            next.set("agent", agentName);
            return next;
          });
        }}
      />

      <div className="flex h-full flex-1 min-w-0 flex-col overflow-hidden">
        {permissionsQuery.data?.level === "bypass" && (
          <div
            role="status"
            className="flex items-center gap-2 border-b border-amber-500/30 bg-amber-500/15 px-4 py-2 text-xs font-medium text-amber-900 dark:text-amber-200"
          >
            <AlertTriangle className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <span>Bypass mode: agents act without asking.</span>
          </div>
        )}

        <div className="space-y-2 p-4 pb-0">
          {showProjectStatus && (
            <ProjectStatus status={project.status} status_detail={project.status_detail} />
          )}
          {selectedAgent?.settings.model_problem && (
            <Alert variant="warning">
              <AlertCircle className="size-4" />
              <AlertTitle>Model configuration</AlertTitle>
              <AlertDescription className="space-y-1">
                <p>{selectedAgent.settings.model_problem}</p>
                <div className="flex gap-4 text-xs">
                  <Link to="/providers" className="font-medium underline hover:text-foreground">
                    AI providers
                  </Link>
                  <Link
                    to={`/projects/${projectId}/settings`}
                    className="font-medium underline hover:text-foreground"
                  >
                    Project settings
                  </Link>
                </div>
              </AlertDescription>
            </Alert>
          )}
          {session.connection === "closed" && session.error && (
            <Alert variant="destructive">
              <AlertDescription className="flex items-center justify-between gap-4">
                <span>{errorText(session.error)}</span>
                <div className="flex items-center gap-3">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void session.reload()}
                  >
                    Retry
                  </Button>
                  <Link to="/" className="font-medium underline">
                    Projects
                  </Link>
                </div>
              </AlertDescription>
            </Alert>
          )}
          {actionError && (
            <Alert variant="destructive">
              <AlertCircle className="size-4" />
              <AlertDescription className="flex items-center justify-between gap-4">
                <span>{actionError}</span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setActionError(null);
                  }}
                >
                  Dismiss
                </Button>
              </AlertDescription>
            </Alert>
          )}
        </div>

        {session.connection === "reconnecting" && (
          <div
            role="status"
            className="bg-amber-500/10 px-4 py-1 text-center text-xs text-amber-700 dark:text-amber-300"
          >
            Reconnecting...
          </div>
        )}

        <div className="flex items-center justify-between border-b px-4 py-2">
          <div className="flex items-center gap-2">
            <AgentSelect
              className="md:hidden"
              agents={chatAgents}
              selectedAgent={selectedAgentName}
              onSelectAgent={(value) => {
                setSearchParams((prev) => {
                  const next = new URLSearchParams(prev);
                  next.set("agent", value);
                  return next;
                });
              }}
            />
            <span className="hidden md:inline text-sm font-semibold text-foreground">
              {selectedAgentDisplayName}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={compactMutation.isPending}
              onClick={() => {
                compactMutation.mutate();
              }}
            >
              Compact now
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={clearMutation.isPending}
              onClick={() => {
                setClearDialogOpen(true);
              }}
            >
              Clear context
            </Button>
          </div>
        </div>

        <MessageList
          messages={session.state?.messages ?? []}
          sessionAgent={selectedAgentName}
          clearedEvents={clearedEventsQuery.data ?? []}
        />

        <Dialog open={clearDialogOpen} onOpenChange={setClearDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Clear conversation context?</DialogTitle>
              <DialogDescription>
                Clear conversation context? The agent will forget earlier messages and start fresh.
                Earlier messages remain visible in the history. An agent working on an active task
                or incident may need to re-read repository context.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setClearDialogOpen(false);
                }}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                disabled={clearMutation.isPending}
                onClick={() => {
                  clearMutation.mutate();
                }}
              >
                Confirm clear
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {currentTask && (
          <TaskBar
            task={currentTask}
            projectId={projectId}
            onDiscard={() => {
              void session.reload();
              void statusQuery.refetch();
            }}
          />
        )}

        <Composer
          agentName={selectedAgentName}
          agentDisplayName={selectedAgentDisplayName}
          busy={session.state?.busy ?? false}
          onSend={(text) => session.send(text)}
          onStop={() => session.stop()}
        />
      </div>
    </div>
  );
}
