import { useQuery } from "@tanstack/react-query";
import { AlertCircle } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router";
import { ProjectStatus } from "@/components/ProjectStatus";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { api, errorText, unwrap } from "@/lib/api";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import type { StreamLike } from "./session/stream";
import { useSession } from "./session/useSession";

export interface AgentsPageProps {
  openStream?: (url: string) => StreamLike;
}

export function AgentsPage({ openStream }: AgentsPageProps = {}) {
  const { projectId } = useParams<{ projectId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  const projectQuery = useQuery({
    queryKey: ["projects", projectId],
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      return unwrap(
        await api.GET("/api/projects/{project_id}", {
          params: { path: { project_id: projectId } },
        }),
      );
    },
    enabled: Boolean(projectId),
  });

  const agentsQuery = useQuery({
    queryKey: ["projects", projectId, "agents"],
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      return unwrap(
        await api.GET("/api/projects/{project_id}/agents", {
          params: { path: { project_id: projectId } },
        }),
      );
    },
    enabled: Boolean(projectId),
  });

  const chatAgents = (agentsQuery.data ?? []).filter((agent) => agent.chat);
  const agentParam = searchParams.get("agent");
  const selectedAgent = chatAgents.find((agent) => agent.name === agentParam) ?? chatAgents[0];

  const selectedAgentName = selectedAgent?.name ?? "orchestrator";
  const selectedAgentDisplayName = selectedAgent?.display_name ?? selectedAgentName;

  const session = useSession(projectId ?? "", selectedAgentName, {
    openStream,
  });

  if (!projectId) return null;

  const project = projectQuery.data;
  const showProjectStatus = project && (project.status !== "ok" || Boolean(project.status_detail));

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="space-y-2 p-4 pb-0">
        {showProjectStatus && (
          <ProjectStatus status={project.status} status_detail={project.status_detail} />
        )}
        {selectedAgent?.settings.model_problem && (
          <Alert
            variant="destructive"
            className="border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-200"
          >
            <AlertCircle className="size-4 text-amber-600 dark:text-amber-400" />
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
              <Link to="/" className="font-medium underline">
                Projects
              </Link>
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
