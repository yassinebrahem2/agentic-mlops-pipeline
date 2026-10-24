import { AlertCircle, AlertTriangle } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router";
import { ProjectStatus } from "@/components/ProjectStatus";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { useAgentStatus, type AgentStatus } from "@/lib/useAgentStatus";
import { useProject } from "@/lib/useProject";
import { useProjectAgents } from "@/lib/useProjectAgents";
import { useProjectPermissions } from "@/lib/useProjectPermissions";
import { AgentList, AgentSelect } from "./AgentList";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { TaskBar } from "./TaskBar";
import { useSession } from "./session/useSession";

export function AgentsPage() {
  const { projectId } = useParams<{ projectId: string }>();
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

  const session = useSession(projectId ?? "", selectedAgentName);

  if (!projectId) return null;

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

  const displayStatuses: AgentStatus[] =
    statusQuery.data && statusQuery.data.length > 0
      ? statusQuery.data
      : (agentsQuery.data ?? []).map((agent) => ({
          agent: agent.name,
          display_name: agent.display_name,
          state: agent.chat ? ("idle" as const) : ("inactive" as const),
          task: null,
          open_prs: [],
          last_finished: null,
        }));

  return (
    <div className="flex h-full overflow-hidden">
      <AgentList
        className="hidden md:flex"
        agents={displayStatuses}
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
        </div>

        {session.connection === "reconnecting" && (
          <div
            role="status"
            className="bg-amber-500/10 px-4 py-1 text-center text-xs text-amber-700 dark:text-amber-300"
          >
            Reconnecting...
          </div>
        )}

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

        <MessageList messages={session.state?.messages ?? []} sessionAgent={selectedAgentName} />

        {session.state?.current_task && (
          <TaskBar
            task={session.state.current_task}
            projectId={projectId}
            onDiscard={() => void session.reload()}
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
