import { Badge } from "@/components/ui/badge";
import type { Dashboard } from "./useDashboard";

export function AgentsList({ agents }: Pick<Dashboard, "agents">) {
  return (
    <section aria-labelledby="agents-title" className="space-y-3">
      <h2 id="agents-title" className="text-lg font-semibold">
        Agents
      </h2>
      {agents.length === 0 ? (
        <p className="text-sm text-muted-foreground">No agents yet.</p>
      ) : (
        <ul className="space-y-2">
          {agents.map((agent) => (
            <li
              key={agent.agent}
              data-testid={`agent-${agent.agent}`}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3 text-sm"
            >
              <div className="min-w-0">
                <p className="font-medium">{agent.agent}</p>
                {agent.doing && (
                  <p className="text-xs text-muted-foreground break-words">{agent.doing}</p>
                )}
              </div>
              <Badge variant={agent.state === "idle" ? "outline" : "secondary"}>
                {agent.state}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
