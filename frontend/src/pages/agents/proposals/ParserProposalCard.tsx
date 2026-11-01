import type { components } from "@/api/schema";
import { Badge } from "@/components/ui/badge";
import type { ApprovalCardComponent } from "../ApprovalCard";
import { ProposalShell } from "./ProposalShell";
import { useProposal } from "./useProposal";

type ParserPayload = components["schemas"]["ParserPayload"];

export const ParserProposalCard: ApprovalCardComponent = ({
  approval,
  projectId,
  toolName,
  className,
}) => {
  const { data: proposal, isLoading, isError } = useProposal(projectId, approval.approval_id);

  const payload =
    proposal && "spec" in proposal.payload ? (proposal.payload as ParserPayload) : null;

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
      {payload && (
        <div className="space-y-4">
          <div className="space-y-3 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-semibold text-foreground">
                {payload.spec.source_id}
              </span>
              <Badge variant="outline" className="text-xs">
                {payload.spec.mapping ? "mapping" : "code"}
              </Badge>
            </div>

            {payload.spec.mapping && (
              <div className="space-y-2 text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-muted-foreground">Format:</span>
                  <Badge variant="secondary" className="font-mono">
                    {payload.spec.mapping.format}
                  </Badge>
                </div>

                {payload.spec.mapping.pattern && (
                  <div className="space-y-1">
                    <span className="font-medium text-muted-foreground">Pattern:</span>
                    <pre className="overflow-x-auto rounded bg-muted p-1.5 font-mono text-[11px]">
                      {payload.spec.mapping.pattern}
                    </pre>
                  </div>
                )}

                <div className="space-y-1">
                  <span className="font-medium text-muted-foreground">Fields:</span>
                  <ul className="space-y-0.5 font-mono text-[11px] text-muted-foreground">
                    {Object.entries(payload.spec.mapping.fields).map(([field, path]) => (
                      <li key={field}>
                        <span className="font-semibold text-foreground">{field}</span> ← {path}
                      </li>
                    ))}
                  </ul>
                </div>

                <div className="space-y-1">
                  <span className="font-medium text-muted-foreground">Fired Condition:</span>
                  <pre className="overflow-x-auto rounded bg-muted p-1.5 font-mono text-[11px]">
                    {JSON.stringify(payload.spec.mapping.fired_when, null, 2)}
                  </pre>
                </div>
              </div>
            )}

            {payload.spec.code && (
              <div className="space-y-2 text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-muted-foreground">Timeout:</span>
                  <span className="font-mono">{payload.spec.code.timeout_s}s</span>
                </div>
                <div className="space-y-1">
                  <span className="font-medium text-muted-foreground">Script:</span>
                  <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-[11px] whitespace-pre">
                    {payload.spec.code.script}
                  </pre>
                </div>
              </div>
            )}
          </div>

          <div className="space-y-2 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
            <h4 className="text-xs font-semibold text-foreground">Sample Input</h4>
            <pre className="max-h-48 overflow-x-auto rounded bg-muted p-2 font-mono text-[11px] whitespace-pre">
              {payload.sample_input}
            </pre>
          </div>

          <div className="space-y-2 rounded-md border border-border/70 bg-card p-3 shadow-2xs">
            <div className="flex items-center justify-between">
              <h4 className="text-xs font-semibold text-foreground">Sample Records</h4>
              <Badge variant="outline" className="text-[10px]">
                {payload.sample_records.length}
              </Badge>
            </div>
            {payload.sample_records.length === 0 ? (
              <p className="text-xs text-muted-foreground">No sample records</p>
            ) : (
              <div className="space-y-2">
                {payload.sample_records.map((rec, i) => (
                  <div
                    key={`${rec.check_id}-${String(i)}`}
                    className="space-y-1 rounded border border-border/50 bg-background p-2 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-foreground">{rec.check_id}</span>
                      <Badge
                        variant={rec.fired ? "destructive" : "secondary"}
                        className="text-[10px]"
                      >
                        {rec.fired ? "Fired" : "Ok"}
                      </Badge>
                    </div>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-[11px] text-muted-foreground">
                      <div>
                        <span className="font-medium">Category:</span> {rec.category}
                      </div>
                      <div>
                        <span className="font-medium">Subject:</span> {rec.subject}
                      </div>
                      {rec.value != null && (
                        <div>
                          <span className="font-medium">Value:</span> {rec.value}
                        </div>
                      )}
                      {rec.threshold != null && (
                        <div>
                          <span className="font-medium">Threshold:</span> {rec.threshold}
                        </div>
                      )}
                      <div>
                        <span className="font-medium">Ran at:</span> {rec.ran_at}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </ProposalShell>
  );
};
