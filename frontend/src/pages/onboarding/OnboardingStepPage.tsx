import { useQuery } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2 } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";
import type { components } from "@/api/schema";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { api, errorText, unwrap } from "@/lib/api";
import { useProject } from "@/lib/useProject";
import { useProjectAgents } from "@/lib/useProjectAgents";
import { useSession } from "@/pages/agents/session/useSession";

type Project = components["schemas"]["Project"];

const ONBOARDING_AGENT = "onboarding";
const NO_DOCKER_SENTENCE =
  "Without Docker, Onboarding can read the repository but not run commands.";

// The one template of the wizard's first message to Onboarding.
function firstMessage(project: Project): string {
  const repository = project.github_full_name ?? project.local_path ?? "";
  return `Please onboard the project "${project.name}". Its repository is ${repository}. Explore it, then ask me what you need to set the project up.`;
}

export function OnboardingStepPage() {
  const { projectId } = useParams<{ projectId: string }>();
  // Every lookup and the session need a project; without one there is nothing to show.
  if (!projectId) return null;
  return <OnboardingStep projectId={projectId} />;
}

function OnboardingStep({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const projectQuery = useProject(projectId);
  const agentsQuery = useProjectAgents(projectId);
  const sandboxQuery = useQuery({
    queryKey: ["projects", projectId, "sandbox"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/sandbox", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
  const session = useSession(projectId, ONBOARDING_AGENT);

  // A ref, not state: a second click in the same tick must not post a second first message.
  const startingRef = useRef(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<unknown>();

  const project = projectQuery.data;
  const agent = agentsQuery.data?.find((entry) => entry.name === ONBOARDING_AGENT);
  const effectiveModel = agent?.settings.effective_model;
  const modelProblem = agent?.settings.model_problem;
  const modelReady = Boolean(effectiveModel) && !modelProblem;
  const agentsPagePath = `/projects/${projectId}/agents?agent=${ONBOARDING_AGENT}`;
  const hasMessages = (session.state?.messages.length ?? 0) > 0;
  // Until the session is loaded, the step cannot tell whether Onboarding was already started.
  const canStart = Boolean(project) && modelReady && session.state !== undefined && !starting;

  const start = async () => {
    if (!project || startingRef.current) return;
    startingRef.current = true;
    setStarting(true);
    setStartError(undefined);
    try {
      await session.send(firstMessage(project));
      void navigate(agentsPagePath);
    } catch (error) {
      setStartError(error);
      startingRef.current = false;
      setStarting(false);
    }
  };

  return (
    <div className="container mx-auto max-w-3xl space-y-6 p-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight">Onboarding</h1>
        <p className="text-sm text-muted-foreground">
          {project
            ? `Onboarding explores ${project.name}, then asks you what it needs to set the project up.`
            : "Onboarding explores the repository, then asks you what it needs to set the project up."}
        </p>
      </div>

      {projectQuery.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(projectQuery.error)}
        </p>
      )}

      <ol className="space-y-4">
        <Step title="Model">
          {agentsQuery.isPending && <p className="text-muted-foreground">Checking the model...</p>}
          {agentsQuery.error && (
            <p role="alert" className="text-destructive">
              {errorText(agentsQuery.error)}
            </p>
          )}
          {agentsQuery.data && modelReady && (
            <p className="flex items-center gap-2">
              <CheckCircle2 className="size-4 text-emerald-600" />
              Ready: {effectiveModel}
            </p>
          )}
          {agentsQuery.data && !modelReady && (
            <div className="space-y-1">
              <p className="flex items-center gap-2 text-amber-700 dark:text-amber-300">
                <AlertCircle className="size-4 shrink-0" />
                {modelProblem ??
                  `No chat model is set for ${agent?.display_name ?? ONBOARDING_AGENT}`}
              </p>
              <Link to="/providers" className="font-medium underline">
                AI providers
              </Link>
            </div>
          )}
        </Step>

        <Step title="Docker">
          {sandboxQuery.isPending && <p className="text-muted-foreground">Checking Docker...</p>}
          {sandboxQuery.error && (
            <p role="alert" className="text-destructive">
              {errorText(sandboxQuery.error)}
            </p>
          )}
          {sandboxQuery.data?.docker.available === true && (
            <p>
              Docker is available
              {sandboxQuery.data.docker.version !== null &&
                ` (version ${sandboxQuery.data.docker.version})`}
              .
            </p>
          )}
          {sandboxQuery.data?.docker.available === false && (
            <div className="space-y-1">
              <p>{sandboxQuery.data.docker.detail ?? "Docker is not available."}</p>
              <p className="text-muted-foreground">{NO_DOCKER_SENTENCE}</p>
            </div>
          )}
        </Step>

        <Step title="Start">
          {session.error && (
            <p role="alert" className="text-destructive">
              {errorText(session.error)}
            </p>
          )}
          {startError !== undefined && (
            <Alert variant="destructive">
              <AlertCircle className="size-4" />
              <AlertDescription>{errorText(startError)}</AlertDescription>
            </Alert>
          )}
          {hasMessages ? (
            <Button asChild>
              <Link to={agentsPagePath}>Open Onboarding</Link>
            </Button>
          ) : (
            <Button type="button" disabled={!canStart} onClick={() => void start()}>
              Start
            </Button>
          )}
        </Step>
      </ol>
    </div>
  );
}

function Step({ title, children }: { title: string; children: ReactNode }) {
  return (
    <li className="space-y-2 rounded-lg border p-4 text-sm">
      <h2 className="text-base font-semibold">{title}</h2>
      {children}
    </li>
  );
}
