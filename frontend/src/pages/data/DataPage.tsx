import { Link, useParams } from "react-router";
import { AlertCircle, Database, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { errorText } from "@/lib/api";
import { useConnections } from "@/lib/useConnections";
import { useHandoff } from "@/lib/useHandoff";
import { ConnectionCard } from "./ConnectionCard";

export function DataPage() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return null;
  return <DataPageView projectId={projectId} />;
}

function DataPageView({ projectId }: { projectId: string }) {
  const {
    data: connections,
    isLoading: connectionsLoading,
    error: connectionsError,
  } = useConnections(projectId);

  const { data: handoff } = useHandoff(projectId);

  return (
    <div className="container max-w-5xl py-6 space-y-6">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <Database className="size-6 text-primary" />
          Data
        </h1>
        <p className="text-sm text-muted-foreground">
          Data connections, tables, and roles across your project.
        </p>
      </div>

      {connectionsError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading connections</AlertTitle>
          <AlertDescription>{errorText(connectionsError)}</AlertDescription>
        </Alert>
      )}

      {connectionsLoading && (
        <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          <span>Loading data connections...</span>
        </div>
      )}

      {!connectionsLoading && !connectionsError && (!connections || connections.length === 0) && (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-sm text-muted-foreground">
            No data connection yet. Add one in{" "}
            <Link
              to={`/projects/${projectId}/settings`}
              className="font-medium underline hover:text-foreground"
            >
              Project settings
            </Link>
            , or approve Onboarding's connections proposal.
          </p>
        </div>
      )}

      {!connectionsLoading && !connectionsError && connections && connections.length > 0 && (
        <div className="space-y-6">
          {connections.map((connection) => (
            <ConnectionCard
              key={connection.id}
              projectId={projectId}
              connection={connection}
              handoff={handoff ?? null}
            />
          ))}
        </div>
      )}

      <footer className="border-t pt-4">
        <p className="text-sm text-muted-foreground">
          Quality and drift history arrives with monitoring.
        </p>
      </footer>
    </div>
  );
}
