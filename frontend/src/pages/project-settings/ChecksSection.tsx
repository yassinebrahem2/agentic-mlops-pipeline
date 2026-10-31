import { useQuery } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Badge } from "@/components/ui/badge";
import { api, unwrap } from "@/lib/api";
import { formatTime } from "@/lib/format";
import { useInvalidateOnEvents } from "@/lib/useInvalidateOnEvents";
import { CATEGORY_LABELS } from "@/pages/incidents/labels";

type State = components["schemas"]["ExpectationState"];

const expectationsKey = (projectId: string) => ["projects", projectId, "expectations"] as const;

// Exhaustive over the generated unions, so a value the backend adds fails to compile here.
const STATUS_LABELS: Record<State["status"], string> = {
  ok: "On time",
  due: "Due",
  late: "Late",
  silent: "Silent",
  suspended: "Suspended by a maintenance window",
  pending_pr: "Waiting for its pull request",
};
const KIND_LABELS: Record<State["key"]["kind"], string> = {
  feed: "Feed",
  job: "Job",
  check: "Check",
  training_request: "Training request",
};

export function ChecksSection({ projectId }: { projectId: string }) {
  const states = useQuery({
    queryKey: expectationsKey(projectId),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/projects/{project_id}/expectations", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
  useInvalidateOnEvents(projectId, expectationsKey(projectId), [
    "check.registered",
    "results.collected",
  ]);
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="checks-title">
      <h2 id="checks-title" className="text-lg font-semibold">
        Checks and schedules
      </h2>
      <p className="text-sm text-muted-foreground">
        What the project expects to arrive, and whether it has.
      </p>
      <QueryStatus query={states} loading="Loading what is expected..." />
      {states.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">Nothing is expected yet.</p>
      )}
      {states.data && states.data.length > 0 && (
        <ul className="space-y-3">
          {states.data.map((state) => (
            <ExpectationRow
              key={`${state.key.kind}/${state.key.ref}/${state.key.model ?? ""}`}
              state={state}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function ExpectationRow({ state }: { state: State }) {
  const { key } = state;
  return (
    <li aria-label={key.ref} className="space-y-2 rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{key.ref}</span>
        <Badge variant="outline">{KIND_LABELS[key.kind]}</Badge>
        <Badge
          variant={
            state.status === "late" || state.status === "silent" ? "destructive" : "secondary"
          }
        >
          {STATUS_LABELS[state.status]}
        </Badge>
        <span className="text-muted-foreground">{state.active ? "Active" : "Inactive"}</span>
      </div>
      <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">Category</dt>
          <dd>{CATEGORY_LABELS[state.category]}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Model</dt>
          <dd>{key.model ?? "–"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Next deadline</dt>
          <dd data-testid="expectation-next-deadline">{formatTime(state.next_deadline)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Last seen</dt>
          <dd data-testid="expectation-last-seen">{formatTime(state.last_seen)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">In this status since</dt>
          <dd data-testid="expectation-since">{formatTime(state.since)}</dd>
        </div>
      </dl>
    </li>
  );
}
