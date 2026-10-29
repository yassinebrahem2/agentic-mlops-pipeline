import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { ConnectionFindings } from "@/components/ConnectionFindings";
import { api, unwrap } from "@/lib/api";
import type { ApprovalCardComponent } from "../ApprovalCard";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

type ConnectionSpec = components["schemas"]["ConnectionSpec"];

interface ConnectionItemProps {
  projectId: string;
  connection: ConnectionSpec;
  isPending: boolean;
}

function ConnectionItem({ projectId, connection, isPending }: ConnectionItemProps) {
  const { data: findings, error: inspectError } = useQuery({
    queryKey: [
      "projects",
      projectId,
      "connections",
      "inspect",
      connection.kind,
      connection.settings,
    ],
    queryFn: async () => {
      return unwrap(
        await api.POST("/api/projects/{project_id}/connections/inspect", {
          params: { path: { project_id: projectId } },
          body: {
            kind: connection.kind,
            settings: { ...connection.settings },
          },
        }),
      );
    },
    enabled: isPending,
  });

  const inspectErrorMessage = inspectError instanceof Error ? inspectError.message : null;

  return (
    <div className="space-y-2 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-foreground">{connection.name}</span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
            {connection.kind}
          </span>
        </div>
      </div>

      <div className="text-xs text-muted-foreground">
        <span className="font-medium text-foreground">Reason:</span> {connection.why}
      </div>

      <div className="space-y-1">
        <div className="text-xs font-medium text-muted-foreground">Settings:</div>
        <ul className="space-y-0.5 text-xs font-mono">
          {Object.entries(connection.settings).map(([key, value]) => (
            <li key={key} className="text-muted-foreground">
              <span className="font-semibold text-foreground">{key}:</span> {value}
            </li>
          ))}
        </ul>
      </div>

      {inspectErrorMessage && (
        <div role="alert" className="text-xs text-destructive">
          {inspectErrorMessage}
        </div>
      )}

      {findings && <ConnectionFindings findings={findings} />}
    </div>
  );
}

/**
 * Proposal card for connection proposals: lists proposed connections with kind, name,
 * settings, reason, inspect findings (via ConnectionFindings) while pending, and applied/failed results.
 */
export const ConnectionsProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

  const isPending = approval.status === "pending" && (!proposal || proposal.status === "pending");

  return (
    <ProposalShell
      approval={approval}
      proposal={proposal}
      isLoading={isLoading}
      isError={isError}
      projectId={projectId}
      toolName={toolName}
      className={className}
    >
      {proposal && "connections" in proposal.payload && (
        <div className="space-y-3">
          {proposal.payload.connections.map((conn) => (
            <ConnectionItem
              key={conn.name}
              projectId={projectId}
              connection={conn}
              isPending={isPending}
            />
          ))}
        </div>
      )}
    </ProposalShell>
  );
};
