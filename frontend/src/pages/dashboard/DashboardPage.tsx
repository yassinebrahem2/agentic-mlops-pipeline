import { useParams } from "react-router";
import { AlertCircle, LayoutDashboard, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { AgentsList } from "./AgentsList";
import { AtRiskList } from "./AtRiskList";
import { ModelCards } from "./ModelCards";
import { Notices } from "./Notices";
import { PipelineMap } from "./PipelineMap";
import { SeverityCounts } from "./SeverityCounts";
import { useDashboard } from "./useDashboard";

export function DashboardPage() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return null;
  return <Dashboard projectId={projectId} />;
}

function Dashboard({ projectId }: { projectId: string }) {
  const query = useDashboard(projectId);
  const view = query.data;

  return (
    <div className="container max-w-6xl py-6 space-y-6">
      <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
        <LayoutDashboard className="size-6 text-primary" />
        Dashboard
      </h1>

      {query.isLoading && (
        <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading dashboard...
        </div>
      )}

      {query.error && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading the dashboard</AlertTitle>
          <AlertDescription className="space-y-2">
            <p>{errorText(query.error)}</p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void query.refetch();
              }}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {view && (
        <>
          <Notices
            heartbeatReachable={view.heartbeat_reachable}
            baselineAvailable={view.baseline_available}
          />
          <PipelineMap stages={view.stages} edges={view.edges} />
          <SeverityCounts counts={view.incidents_by_severity} />
          <ModelCards models={view.models} />
          <div className="grid gap-6 lg:grid-cols-2">
            <AtRiskList items={view.at_risk} />
            <AgentsList agents={view.agents} />
          </div>
        </>
      )}
    </div>
  );
}
