import type { ReactNode } from "react";
import type { components } from "@/api/schema";
import { CommandsTable, type CommandSpec } from "./CommandsTable";
import { formatCadence } from "./formatCadence";

export { CommandsTable, type CommandSpec };

export type ModelSpec = components["schemas"]["ModelSpec"];
export type DataRole = components["schemas"]["DataRole"];
export type TrackingSourceSpec = components["schemas"]["TrackingSourceSpec"];
export type PipelineStage = components["schemas"]["PipelineStage"];
export type PipelineEdge = components["schemas"]["PipelineEdge"];
export type Operations = components["schemas"]["Operations"];
export type Gap = components["schemas"]["Gap"];
export type Grants = components["schemas"]["Grants"];
type FileResults = components["schemas"]["FileResults"];
type CiResults = components["schemas"]["CiResults"];
type MlflowResults = components["schemas"]["MlflowResults"];
type DataFeedRef = components["schemas"]["DataFeedRef"];
type JobFeedRef = components["schemas"]["JobFeedRef"];

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <span className="font-medium text-foreground">{label}:</span> {children}
    </div>
  );
}

function renderResults(r: FileResults | CiResults | MlflowResults) {
  if ("path_pattern" in r) return `${r.connection}:${r.path_pattern}`;
  if ("workflow" in r) return r.workflow;
  return `${r.metric} (${r.fires_when === "below" ? "<" : ">"} ${String(r.threshold)})`;
}

function feedRef(ref: DataFeedRef | JobFeedRef) {
  if ("table" in ref) return `${ref.connection}.${ref.table}`;
  if (ref.ci_workflow) return ref.ci_workflow;
  if (ref.file) return `${ref.file.connection}:${ref.file.path_pattern}`;
  return "none";
}

export function ModelsSection({ models }: { models: ModelSpec[] }) {
  return (
    <section aria-label="Models" className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Models
      </h4>
      {models.length === 0 ? (
        <p className="text-xs text-muted-foreground">none</p>
      ) : (
        <div className="space-y-3">
          {models.map((model) => (
            <div
              key={model.name}
              className="space-y-2 rounded-md border border-border/60 bg-card p-3 shadow-2xs text-xs"
            >
              <div className="flex items-center justify-between border-b border-border/40 pb-1.5">
                <span className="font-semibold text-foreground text-sm">{model.name}</span>
                <span className="text-muted-foreground">
                  Promotion: <span className="font-medium text-foreground">{model.promotion}</span>
                </span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1 text-muted-foreground">
                <Field label="Objective">{model.profile.objective}</Field>
                <Field label="Problem Type">{model.profile.problem_type}</Field>
                <Field label="Target">{model.profile.target_column}</Field>
                <Field label="Positive Class">{model.profile.positive_class ?? "none"}</Field>
                <Field label="Primary Metric">{model.profile.primary_metric}</Field>
                <Field label="Metric Rationale">{model.profile.metric_rationale}</Field>
                <Field label="Operating Threshold">
                  {model.profile.operating_threshold != null
                    ? String(model.profile.operating_threshold)
                    : "none"}
                </Field>
                <Field label="Label Delay Days">{String(model.profile.label_delay_days)}</Field>
                <Field label="Label Maturity Days">
                  {String(model.profile.label_maturity_days)}
                </Field>
                <Field label="Serving Mode">{model.profile.serving_mode}</Field>
                <Field label="Error Costs">{model.profile.error_costs}</Field>
                <Field label="Cost Ratio">
                  {model.profile.cost_ratio != null ? String(model.profile.cost_ratio) : "none"}
                </Field>
                <div className="col-span-full">
                  <Field label="Groups">
                    {model.profile.groups.length === 0
                      ? "none"
                      : model.profile.groups
                          .map(
                            (g) =>
                              `${g.column}: ${g.values && g.values.length > 0 ? g.values.join(", ") : "all"}`,
                          )
                          .join("; ")}
                  </Field>
                </div>
                <Field label="Predictions">
                  {model.predictions
                    ? `${model.predictions.connection}.${model.predictions.table}`
                    : "none"}
                </Field>
                <Field label="Labels">
                  {model.labels ? `${model.labels.connection}.${model.labels.table}` : "none"}
                </Field>
                <Field label="Environments">
                  {Object.keys(model.environments).length === 0
                    ? "none"
                    : Object.entries(model.environments)
                        .map(([alias, env]) => `${alias}: ${env}`)
                        .join(", ")}
                </Field>
                <Field label="Production Signal">
                  {model.production_signal
                    ? `${model.production_signal.kind} (${model.production_signal.ref})`
                    : "none"}
                </Field>
              </div>
              <div className="space-y-1 pt-1">
                <span className="font-medium text-foreground">Commands:</span>
                <CommandsTable commands={model.commands} />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function CommandsSection({ commands }: { commands: CommandSpec[] }) {
  return (
    <section aria-label="Commands" className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Commands
      </h4>
      <CommandsTable commands={commands} />
    </section>
  );
}

export function DataRolesSection({ dataRoles }: { dataRoles: DataRole[] }) {
  return (
    <section aria-label="Data Roles" className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Data Roles
      </h4>
      {dataRoles.length === 0 ? (
        <p className="text-xs text-muted-foreground">none</p>
      ) : (
        <div className="overflow-x-auto rounded border border-border/60 bg-muted/20">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-border/60 bg-muted/40 font-semibold text-foreground">
              <tr>
                <th className="p-1.5">Connection</th>
                <th className="p-1.5">Table</th>
                <th className="p-1.5">Role</th>
                <th className="p-1.5">ID Column</th>
                <th className="p-1.5">Time Column</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40 text-muted-foreground">
              {dataRoles.map((role, idx) => (
                <tr key={`${role.connection}-${role.table}-${String(idx)}`}>
                  <td className="p-1.5 font-medium text-foreground">{role.connection}</td>
                  <td className="p-1.5">{role.table}</td>
                  <td className="p-1.5">{role.role}</td>
                  <td className="p-1.5">{role.id_column ?? "none"}</td>
                  <td className="p-1.5">{role.time_column ?? "none"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function TrackingSection({ tracking }: { tracking: TrackingSourceSpec[] }) {
  return (
    <section aria-label="Tracking" className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Tracking
      </h4>
      {tracking.length === 0 ? (
        <p className="text-xs text-muted-foreground">none</p>
      ) : (
        <div className="space-y-2">
          {tracking.map((source, idx) => (
            <div
              key={`${source.type}-${String(idx)}`}
              className="space-y-1 rounded border border-border/60 bg-muted/20 p-2 text-xs text-muted-foreground"
            >
              <Field label="Type">{source.type}</Field>
              <Field label="Settings">
                {"url" in source.settings
                  ? source.settings.url
                  : `${source.settings.repository} (${source.settings.workflows.join(", ")})`}
              </Field>
              <Field label="Auth">{source.auth}</Field>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function PipelineSection({
  pipeline,
  edges,
}: {
  pipeline: PipelineStage[];
  edges: PipelineEdge[];
}) {
  const isEmpty = pipeline.length === 0 && edges.length === 0;
  return (
    <section aria-label="Pipeline" className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Pipeline
      </h4>
      {isEmpty ? (
        <p className="text-xs text-muted-foreground">none</p>
      ) : (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <div className="text-xs font-medium text-foreground">Stages:</div>
            {pipeline.length === 0 ? (
              <p className="text-xs text-muted-foreground">none</p>
            ) : (
              <div className="space-y-1">
                {pipeline.map((stage) => (
                  <div
                    key={stage.name}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-border/60 bg-muted/20 p-2 text-xs text-muted-foreground"
                  >
                    <span className="font-semibold text-foreground">{stage.name}</span>
                    <span className="font-mono text-[11px] text-foreground">({stage.entry})</span>
                    <span>
                      Inputs: {stage.inputs.length === 0 ? "none" : stage.inputs.join(", ")}
                    </span>
                    <span>
                      Outputs: {stage.outputs.length === 0 ? "none" : stage.outputs.join(", ")}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="space-y-1.5">
            <div className="text-xs font-medium text-foreground">Edges:</div>
            {edges.length === 0 ? (
              <p className="text-xs text-muted-foreground">none</p>
            ) : (
              <ul className="list-disc list-inside space-y-0.5 text-xs text-muted-foreground">
                {edges.map((edge, idx) => (
                  <li key={`${edge.from_stage}-${edge.to_stage}-${String(idx)}`}>
                    <span className="font-medium text-foreground">
                      {edge.from_stage} to {edge.to_stage}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

export function OperationsSection({ operations }: { operations: Operations }) {
  const resLoc = operations.results_location;
  return (
    <section aria-label="Operations" className="space-y-2 text-xs">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Operations
      </h4>
      <div className="rounded border border-border/60 bg-muted/20 p-2 text-muted-foreground">
        <Field label="Scheduler">
          {operations.scheduler
            ? `${operations.scheduler.name} (${operations.scheduler.config_location})`
            : "none"}
        </Field>
      </div>
      <div className="space-y-1">
        <span className="font-semibold text-foreground">Feeds:</span>
        {operations.feeds.length === 0 ? (
          <p className="text-muted-foreground pl-2">none</p>
        ) : (
          <div className="space-y-1">
            {operations.feeds.map((feed) => (
              <div
                key={feed.name}
                className="space-y-0.5 rounded border border-border/60 bg-muted/20 p-2 text-muted-foreground"
              >
                <div className="font-semibold text-foreground">{feed.name}</div>
                <div>
                  Kind: {feed.kind} | Reference: {feedRef(feed.ref)}
                </div>
                <div>
                  Cadence: {formatCadence(feed.cadence)} | Grace: {String(feed.grace_minutes)}m |
                  Timezone: {feed.timezone}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="space-y-1">
        <span className="font-semibold text-foreground">On-Call:</span>
        {operations.on_call.length === 0 ? (
          <p className="text-muted-foreground pl-2">none</p>
        ) : (
          <div className="space-y-1">
            {operations.on_call.map((oc) => (
              <div
                key={oc.name}
                className="space-y-0.5 rounded border border-border/60 bg-muted/20 p-2 text-muted-foreground"
              >
                <div className="font-semibold text-foreground">{oc.name}</div>
                <div>
                  Contacts:{" "}
                  {oc.contacts.length === 0
                    ? "none"
                    : oc.contacts.map((c) => `${c.channel}: ${c.address}`).join(", ")}
                </div>
                <div>
                  Hours: {oc.hours.days.join(", ")} ({oc.hours.start} - {oc.hours.end}) | Timezone:{" "}
                  {oc.timezone} | Escalation: {String(oc.escalation_order)}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="space-y-1">
        <span className="font-semibold text-foreground">Checks:</span>
        {operations.checks.length === 0 ? (
          <p className="text-muted-foreground pl-2">none</p>
        ) : (
          <div className="space-y-1">
            {operations.checks.map((chk) => (
              <div
                key={chk.check_id}
                className="space-y-0.5 rounded border border-border/60 bg-muted/20 p-2 text-muted-foreground"
              >
                <div className="font-semibold text-foreground">
                  {chk.name}{" "}
                  <span className="text-[11px] font-normal text-muted-foreground font-mono">
                    ({chk.check_id})
                  </span>
                </div>
                <div>
                  Category: {chk.category} | Cadence:{" "}
                  {chk.cadence ? formatCadence(chk.cadence) : "not judged for silence"} | Timezone:{" "}
                  {chk.timezone}
                </div>
                <div>Results: {renderResults(chk.results)}</div>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="rounded border border-border/60 bg-muted/20 p-2 text-muted-foreground">
        <Field label="Results Location">{renderResults(resLoc)}</Field>
      </div>
    </section>
  );
}

export function GapsSection({ gaps }: { gaps: Gap[] }) {
  return (
    <section aria-label="Gaps" className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Gaps</h4>
      {gaps.length === 0 ? (
        <p className="text-xs text-muted-foreground">none</p>
      ) : (
        <div className="space-y-1.5 text-xs">
          {gaps.map((gap, idx) => (
            <div
              key={String(idx)}
              className="space-y-0.5 rounded border border-border/60 bg-muted/20 p-2 text-muted-foreground"
            >
              <Field label="What">{gap.what}</Field>
              <Field label="Next Step">{gap.next_step}</Field>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function GrantsSection({ grants }: { grants: Grants }) {
  return (
    <section aria-label="Grants" className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Grants
      </h4>
      <div className="space-y-1 rounded border border-border/60 bg-muted/20 p-2 text-xs text-muted-foreground">
        <Field label="Network">{grants.network ? "on" : "off"}</Field>
        <Field label="Permission Level">{grants.permission_level}</Field>
      </div>
    </section>
  );
}
