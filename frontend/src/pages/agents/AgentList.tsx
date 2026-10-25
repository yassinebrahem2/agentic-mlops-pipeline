import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { errorText } from "@/lib/api";
import { STATE_CONFIG, type AgentStatus } from "@/lib/useAgentStatus";
import { cn } from "@/lib/utils";

export interface AgentListProps {
  agents: AgentStatus[];
  isLoading?: boolean;
  error?: unknown;
  selectedAgent: string;
  onSelectAgent: (name: string) => void;
  className?: string;
}

export function AgentList({
  agents,
  isLoading,
  error,
  selectedAgent,
  onSelectAgent,
  className,
}: AgentListProps) {
  return (
    <aside
      className={cn("w-64 shrink-0 flex-col border-r bg-muted/10", className)}
      aria-label="Agents"
    >
      <div className="border-b px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight text-foreground">Agents</h2>
      </div>
      {isLoading ? (
        <div className="p-4 text-xs text-muted-foreground">Loading agents...</div>
      ) : error ? (
        <div className="p-4 text-xs text-destructive">
          {error instanceof Error ? error.message : errorText(error)}
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-2 space-y-1" role="list">
          {agents.map((agent) => {
            const isInactive = agent.state === "inactive";
            const isSelected = selectedAgent === agent.agent;
            const config = STATE_CONFIG[agent.state];
            const detailText = agent.last_finished || config.detail;

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
                      className={cn("size-2 rounded-full", config.dotClass)}
                      aria-hidden="true"
                    />
                    <span>{config.label}</span>
                    {isInactive && detailText && <span className="truncate">({detailText})</span>}
                  </div>
                </button>
              </div>
            );
          })}
        </div>
      )}
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
  return (
    <div className={cn("p-4 pb-0 flex items-center gap-2", className)}>
      <label htmlFor="agent-switcher" className="text-xs font-medium text-muted-foreground">
        Agent:
      </label>
      <Select value={selectedAgent} onValueChange={onSelectAgent}>
        <SelectTrigger id="agent-switcher" aria-label="Select agent" className="w-[180px] h-8">
          <SelectValue placeholder="Select an agent" />
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
