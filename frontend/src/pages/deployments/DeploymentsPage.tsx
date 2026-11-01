import { useState, type ReactNode } from "react";
import { useParams } from "react-router";
import { Rocket } from "lucide-react";
import { QueryStatus } from "@/components/QueryStatus";
import { useProject } from "@/lib/useProject";
import { DecisionsTable } from "./DecisionsTable";
import { EnvironmentsTable } from "./EnvironmentsTable";
import { OpenPrsList } from "./OpenPrsList";
import { ReportDialog } from "./ReportDialog";
import { useDeployments } from "./useDeployments";

export function DeploymentsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return null;
  return <Deployments projectId={projectId} />;
}

function Section({
  title,
  isEmpty,
  empty,
  children,
}: {
  title: string;
  isEmpty: boolean;
  empty: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">{title}</h2>
      {isEmpty ? <p className="text-sm text-muted-foreground">{empty}</p> : children}
    </section>
  );
}

function Deployments({ projectId }: { projectId: string }) {
  const query = useDeployments(projectId);
  const { data: project } = useProject(projectId);
  const [open, setOpen] = useState<{ reportId: string; model: string } | undefined>(undefined);
  const view = query.data;
  const nothingDeployed =
    view !== undefined &&
    view.environments.length === 0 &&
    view.decisions.length === 0 &&
    view.open_prs.length === 0;

  return (
    <div className="container max-w-4xl py-6 space-y-6">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <Rocket className="size-6 text-primary" />
          Deployments
        </h1>
        <p className="text-sm text-muted-foreground">
          Which version of each model runs where, how it got there, and the pipeline changes still
          open.
        </p>
      </div>

      <QueryStatus query={query} loading="Loading deployments..." />

      {nothingDeployed && (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-sm text-muted-foreground">
            Nothing is deployed yet. Environments and history appear after a model handoff and its
            first promotion.
          </p>
        </div>
      )}

      {view && !nothingDeployed && (
        <>
          <Section
            title="Environments"
            isEmpty={view.environments.length === 0}
            empty="No environments to show yet."
          >
            <EnvironmentsTable rows={view.environments} />
          </Section>
          <Section
            title="History"
            isEmpty={view.decisions.length === 0}
            empty="No promotions or rollbacks yet."
          >
            <DecisionsTable
              rows={view.decisions}
              onOpenReport={(row) => {
                setOpen({ reportId: row.report_id, model: row.model });
              }}
            />
          </Section>
          <Section
            title="Open pull requests"
            isEmpty={view.open_prs.length === 0}
            empty="No open pull requests from the agents."
          >
            <OpenPrsList prs={view.open_prs} githubFullName={project?.github_full_name} />
          </Section>
        </>
      )}

      {open && (
        <ReportDialog
          projectId={projectId}
          reportId={open.reportId}
          model={open.model}
          onClose={() => {
            setOpen(undefined);
          }}
        />
      )}
    </div>
  );
}
