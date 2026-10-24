import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Client } from "openapi-fetch";
import type { components } from "@/api/schema";
import { api, unwrap, type paths as GeneratedPaths } from "@/lib/api";
import { useProjectEvents } from "@/lib/useProjectEvents";

export type AgentState = "idle" | "running" | "waiting_approval" | "waiting_reply" | "inactive";

export type PrRef = {
  number: number;
  url: string;
  title: string;
};

export type TaskView = components["schemas"]["TaskView"];

export type AgentStatus = {
  agent: string;
  display_name: string;
  state: AgentState;
  task: TaskView | null;
  open_prs: PrRef[];
  last_finished?: string | null;
};

export type AgentStatusPaths = GeneratedPaths & {
  "/api/projects/{project_id}/agents/status": {
    get: {
      parameters: {
        path: {
          project_id: string;
        };
      };
      responses: {
        200: {
          content: {
            "application/json": AgentStatus[];
          };
        };
      };
    };
  };
};

export function agentStatusQueryKey(projectId: string | undefined) {
  return ["projects", projectId, "agents", "status"] as const;
}

/**
 * Loads agent statuses via GET /api/projects/{projectId}/agents/status.
 * Automatically refetches (debounced by 150ms) on each project event from SSE.
 */
export function useAgentStatus(projectId: string | undefined) {
  const queryClient = useQueryClient();
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const query = useQuery({
    queryKey: agentStatusQueryKey(projectId),
    queryFn: async () => {
      if (!projectId) throw new Error("No projectId");
      const client = api as unknown as Client<AgentStatusPaths>;
      const result = await client.GET("/api/projects/{project_id}/agents/status", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(result);
    },
    enabled: Boolean(projectId),
  });

  useProjectEvents(projectId, {
    onEvent: () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        void queryClient.invalidateQueries({ queryKey: agentStatusQueryKey(projectId) });
      }, 150);
    },
    onResync: () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        void queryClient.invalidateQueries({ queryKey: agentStatusQueryKey(projectId) });
      }, 150);
    },
  });

  useEffect(() => {
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);

  return query;
}
