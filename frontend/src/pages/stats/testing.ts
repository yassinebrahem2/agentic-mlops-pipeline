import { vi } from "vitest";
import type { components } from "@/api/schema";
import * as streamModule from "@/lib/stream";
import { TestFakeStream } from "@/pages/inbox/testing";
import { project } from "@/test/projects";

type StatsView = components["schemas"]["StatsView"];
type SilentTeamAlert = components["schemas"]["SilentTeamAlert"];

export const STATS_PATH = "/projects/prj_1/stats";

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export const sampleStats: StatsView = {
  window: "30d",
  category: null,
  baseline_available: true,
  groups: {
    detection: {
      precision: { value: 0.8, n: 10, collecting: false },
      misses: { value: 3, n: 3, collecting: false },
      // The page reads the flag, never a threshold: 12 verdicts can still be collecting.
      caught_baseline_missed: { value: 2, n: 12, collecting: true },
    },
    speed: {
      detection_delay_s: { value: 90, n: 6, collecting: false },
      time_to_resolution_s: { value: null, n: 0, collecting: true },
    },
    noise: {
      // Two incidents and not collecting: the value shows.
      alerts_per_week: { value: 2.5, n: 2, collecting: false },
    },
    cost: {
      cost_per_incident: { value: 1.5, n: 4, collecting: false },
      tokens_per_agent: { value: 1234.6, n: 3, collecting: false },
    },
    reliability: {
      failed_runs: { value: null, n: 0, collecting: false },
    },
    future_group: {
      brand_new_metric: { value: 7, n: 7, collecting: false },
    },
  },
};

export const sampleAlerts: SilentTeamAlert[] = [
  {
    result_id: "res_1",
    check_id: "team_null_rate",
    subject: "orders.amount",
    ran_at: "2026-10-30T09:00:00Z",
    verdict: null,
  },
  {
    result_id: "res_2",
    check_id: "team_latency",
    subject: "scoring-api",
    ran_at: "2026-10-29T08:00:00Z",
    verdict: "real_issue",
  },
];

export interface ApiCalls {
  stats: URL[];
  silent: number;
  verdicts: { resultId: string; body: string }[];
}

type Answer = () => Response | Promise<Response>;

const VERDICT_PATH = /^\/api\/projects\/prj_1\/baseline\/silent-alerts\/([^/]+)\/verdict$/;

/** Stubs `fetch` for the Stats page and the shell around it, and the shell's event stream. */
export function stubApi(answers: { stats?: Answer; silent?: Answer; verdict?: Answer }): ApiCalls {
  const calls: ApiCalls = { stats: [], silent: 0, verdicts: [] };
  vi.spyOn(streamModule, "openStream").mockImplementation(() => new TestFakeStream());
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const url = new URL(input.url);
      const path = url.pathname;
      if (path === "/api/projects") return json([project("prj_1", "Churn model", null)]);
      if (input.method === "GET" && path === "/api/projects/prj_1/stats") {
        calls.stats.push(url);
        return (answers.stats ?? (() => json(sampleStats)))();
      }
      if (input.method === "GET" && path === "/api/projects/prj_1/baseline/silent-alerts") {
        calls.silent += 1;
        return (answers.silent ?? (() => json([])))();
      }
      const resultId = VERDICT_PATH.exec(path)?.[1];
      if (input.method === "POST" && resultId !== undefined) {
        calls.verdicts.push({ resultId, body: await input.text() });
        return (answers.verdict ?? (() => json({})))();
      }
      throw new Error(`Unexpected ${input.method} ${path}`);
    }),
  );
  return calls;
}
