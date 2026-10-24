import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { AgentState, AgentStatus } from "@/lib/useAgentStatus";
import { cn } from "@/lib/utils";

function getStateLabel(state: AgentState): string {
  switch (state) {
    case "idle":
      return "idle";
    case "running":
      return "running";
    case "waiting_approval":
      return "waiting for approval";
    case "waiting_reply":
      return "waiting for a reply";
    case "inactive":
      return "inactive";
  }
}

function getStateDotClass(state: AgentState): string {
  switch (state) {
    case "idle":
      return "bg-emerald-500";
    case "running":
      return "bg-blue-500 animate-pulse";
    case "waiting_approval":
      return "bg-amber-500";
    case "waiting_reply":
      return "bg-indigo-500";
    case "inactive":
      return "bg-muted-foreground/40";
  }
}

export interface AgentListProps {
  agents: AgentStatus[];
  selectedAgent: string;
  onSelectAgent: (name: string) => void;
  className?: string;
}

export function AgentList({ agents, selectedAgent, onSelectAgent, className }: AgentListProps) {
  return (
    <aside
      className={cn("w-64 shrink-0 flex-col border-r bg-muted/10", className)}
      aria-label="Agents"
    >
      <div className="border-b px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight text-foreground">Agents</h2>
      </div>
      <div className="flex-1 overflow-y-auto p-2 space-y-1" role="list">
        {agents.map((agent) => {
          const isInactive = agent.state === "inactive";
          const isSelected = selectedAgent === agent.agent;
          const statusText = agent.last_finished || "starts in a later stage";

          return (
            <div key={agent.agent} role="listitem">
              <button
                type="button"
                onClick={() => {
                  if (!isInactive) {
                    onSelectAgent(agent.agent);
                  }
                }}
                disabled={isInactive}
                aria-disabled={isInactive}
                aria-current={isSelected ? "true" : undefined}
                className={cn(
                  "flex w-full flex-col items-start gap-1 rounded-md px-3 py-2 text-left text-sm transition-colors",
                  isSelected && !isInactive
                    ? "bg-accent text-accent-foreground font-medium"
                    : "text-foreground hover:bg-muted/60",
                  isInactive && "cursor-not-allowed opacity-60 hover:bg-transparent",
                )}
              >
                <div className="flex w-full items-center justify-between gap-2">
                  <span className="truncate">{agent.display_name}</span>
                  {agent.state === "waiting_approval" && (
                    <Badge
                      variant="outline"
                      className="border-amber-500/40 text-amber-600 dark:text-amber-400 text-[10px] px-1.5 py-0 h-4"
                    >
                      Approval
                    </Badge>
                  )}
                </div>
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <span
                    className={cn("size-2 rounded-full", getStateDotClass(agent.state))}
                    aria-hidden="true"
                  />
                  <span>{getStateLabel(agent.state)}</span>
                  {isInactive && statusText && statusText !== "inactive" && (
                    <span className="truncate">({statusText})</span>
                  )}
                </div>
              </button>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

export interface AgentSelectProps {
  agents: Array<{ name: string; display_name: string }>;
  selectedAgent: string;
  onSelectAgent: (name: string) => void;
  className?: string;
}

export function AgentSelect({ agents, selectedAgent, onSelectAgent, className }: AgentSelectProps) {
  if (agents.length <= 1) return null;

  return (
    <div className={cn("flex items-center gap-2 border-b px-4 py-2 text-sm", className)}>
      <label htmlFor="agent-select" className="font-medium">
        Agent:
      </label>
      <Select value={selectedAgent} onValueChange={onSelectAgent}>
        <SelectTrigger id="agent-select" aria-label="Select agent" size="sm">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {agents.map((agent) => (
            <SelectItem key={agent.name} value={agent.name}>
              {agent.display_name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
