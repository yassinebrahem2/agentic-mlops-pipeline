import { AlertCircle } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router";
import { ProjectStatus } from "@/components/ProjectStatus";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { useProject } from "@/lib/useProject";
import { useProjectAgents } from "@/lib/useProjectAgents";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { useSession } from "./session/useSession";

export function AgentsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  const projectQuery = useProject(projectId);
  const agentsQuery = useProjectAgents(projectId);

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

  return (
    <div className="flex h-full flex-col overflow-hidden">
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

      {chatAgents.length > 1 && (
        <div className="flex items-center gap-2 border-b px-4 py-2 text-sm">
          <label htmlFor="agent-select" className="font-medium">
            Agent:
          </label>
          <select
            id="agent-select"
            aria-label="Select agent"
            value={selectedAgentName}
            onChange={(e) => {
              setSearchParams((prev) => {
                const next = new URLSearchParams(prev);
                next.set("agent", e.target.value);
                return next;
              });
            }}
            className="rounded border bg-background px-2 py-1 text-sm"
          >
            {chatAgents.map((agent) => (
              <option key={agent.name} value={agent.name}>
                {agent.display_name}
              </option>
            ))}
          </select>
        </div>
      )}

      <MessageList messages={session.state?.messages ?? []} />

      <Composer
        agentName={selectedAgentName}
        agentDisplayName={selectedAgentDisplayName}
        busy={session.state?.busy ?? false}
        onSend={(text) => session.send(text)}
        onStop={() => session.stop()}
      />
    </div>
  );
}
