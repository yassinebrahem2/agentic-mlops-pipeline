import { vi, type MockInstance } from "vitest";
import type { PageRoute } from "@/app/routes";
import { api } from "@/lib/api";
import { queryClient } from "@/lib/query";
import { route } from "./route";
import * as streamModule from "./session/stream";
import {
  FakeStream,
  STREAM_CLOSED,
  STREAM_CONNECTING,
  STREAM_OPEN,
  type StreamLike,
} from "./session/testing";

export { FakeStream, STREAM_CLOSED, STREAM_CONNECTING, STREAM_OPEN, type StreamLike };

export const testRoutes: PageRoute[] = [
  route,
  { path: "/", element: <div>Projects</div> },
  { path: "/providers", element: <div>AI providers</div> },
  {
    path: "/projects/:projectId/settings",
    element: <div>Project settings</div>,
  },
];

export interface DefaultMockOverrides {
  projectStatus?: "ok" | "missing" | "empty" | "clone_failed";
  statusDetail?: string | null;
  modelProblem?: string | null;
  busy?: boolean;
  messages?: unknown[];
  agents?: unknown[];
  agentsStatus?: unknown[];
  permissionLevel?: "ask" | "bypass";
  currentTask?: unknown;
}

export function setupAgentsOpenStreamMock(fakeStream: FakeStream): MockInstance {
  queryClient.clear();
  return vi.spyOn(streamModule, "openStream").mockImplementation(() => {
    setTimeout(() => {
      fakeStream.emitOpen();
    }, 0);
    return fakeStream;
  });
}

export function setupDefaultMocks(overrides?: DefaultMockOverrides): {
  getSpy: MockInstance;
  postSpy: MockInstance;
} {
  const getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
    if (path === "/api/projects/{project_id}") {
      return Promise.resolve({
        data: {
          id: "prj_1",
          name: "Test Project",
          status: overrides?.projectStatus ?? "ok",
          status_detail: overrides?.statusDetail ?? null,
          source: "local",
          path: "/test",
          last_opened: "2026-10-21T00:00:00Z",
        },
        response: new Response(null, { status: 200 }),
      });
    }
    if (path === "/api/projects/{project_id}/agents") {
      return Promise.resolve({
        data: overrides?.agents ?? [
          {
            name: "orchestrator",
            display_name: "Orchestrator",
            chat: true,
            settings: {
              model_problem: overrides?.modelProblem ?? null,
            },
          },
        ],
        response: new Response(null, { status: 200 }),
      });
    }
    if (path === "/api/projects/{project_id}/agents/status") {
      const defaultAgents = overrides?.agents ?? [
        {
          name: "orchestrator",
          display_name: "Orchestrator",
          chat: true,
          settings: {
            model_problem: overrides?.modelProblem ?? null,
          },
        },
      ];
      const statusData =
        overrides?.agentsStatus ??
        (defaultAgents as Array<{ name: string; display_name: string; chat?: boolean }>).map(
          (a) => ({
            agent: a.name,
            display_name: a.display_name,
            state: a.chat === false ? "inactive" : "idle",
            task: null,
            open_prs: [],
            last_finished: null,
          }),
        );
      return Promise.resolve({
        data: statusData,
        response: new Response(null, { status: 200 }),
      });
    }
    if (path === "/api/projects/{project_id}/settings/permissions") {
      return Promise.resolve({
        data: {
          level: overrides?.permissionLevel ?? "ask",
        },
        response: new Response(null, { status: 200 }),
      });
    }
    if (path === "/api/projects/{project_id}/agents/{agent}/session") {
      return Promise.resolve({
        data: {
          id: "s1",
          agent: "orchestrator",
          busy: overrides?.busy ?? false,
          messages: overrides?.messages ?? [],
          last_seq: 10,
          current_task: overrides?.currentTask ?? null,
        },
        response: new Response(null, { status: 200 }),
      });
    }
    return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
  });

  const postSpy = vi.spyOn(api, "POST").mockImplementation(() => {
    return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
  });

  return { getSpy, postSpy };
}
