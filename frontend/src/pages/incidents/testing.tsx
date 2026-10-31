import { vi, type MockInstance } from "vitest";
import type { components } from "@/api/schema";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { TestFakeStream } from "@/pages/inbox/testing";
import { project, stubProjects } from "@/test/projects";

type Schemas = components["schemas"];
export type IncidentFixture = Schemas["IncidentView"];
export type SignalFixture = Schemas["SignalView"];
export type EventFixture = Schemas["EventRecord"];

export const INCIDENTS = "/api/projects/{project_id}/incidents";
export const INCIDENT = "/api/projects/{project_id}/incidents/{incident_id}";
export const SIGNALS = "/api/projects/{project_id}/incidents/{incident_id}/signals";
export const TIMELINE = "/api/projects/{project_id}/incidents/{incident_id}/timeline";
export const ACKNOWLEDGE = "/api/projects/{project_id}/incidents/{incident_id}/acknowledge";
export const CLOSE = "/api/projects/{project_id}/incidents/{incident_id}/close";
export const VERDICT = "/api/projects/{project_id}/incidents/{incident_id}/verdict";
export const MISSES = "/api/projects/{project_id}/misses";

export function incident(id: string, overrides: Partial<IncidentFixture> = {}): IncidentFixture {
  return {
    id,
    category: "drift",
    subtype: null,
    severity: "high",
    status: "open",
    cause: "Feature age drifted",
    cause_key: `k_${id}`,
    model: "churn",
    signal_ids: [],
    prs: [],
    issues: [],
    held: false,
    merged_into: null,
    started_at: "2026-10-30T09:00:00Z",
    detected_at: "2026-10-30T09:05:00Z",
    acknowledged_at: null,
    mitigated_at: null,
    resolved_at: null,
    closed_at: null,
    snoozed_until: null,
    resolved_offline: false,
    reopened_count: 0,
    verdict: null,
    ...overrides,
  };
}

export function signal(id: string, overrides: Partial<SignalFixture> = {}): SignalFixture {
  return {
    id,
    incident_id: "inc_1",
    kind: "fired_result",
    category: "drift",
    subject: "features.age",
    model: "churn",
    ran_at: "2026-10-30T09:00:00Z",
    seen_at: "2026-10-30T09:01:00Z",
    detail: { psi: 0.31 },
    result_id: "res_1",
    source_ref: "ci/run/42",
    expectation: null,
    cleared: false,
    cause_key: "k_inc_1",
    ...overrides,
  };
}

export function eventRecord(seq: number, type: EventFixture["type"], payload = {}): EventFixture {
  return {
    id: `evt_${String(seq)}`,
    seq,
    ts: `2026-10-30T09:${String(seq).padStart(2, "0")}:00Z`,
    source: "monitor",
    type,
    subject_id: "inc_1",
    payload,
  };
}

export function ok(data: unknown, status = 200) {
  return Promise.resolve({ data, response: new Response(null, { status }) });
}

export function failure(code: string, message: string, status: number, detail?: unknown) {
  return Promise.reject(new ApiError(code, message, status, detail));
}

export type Handlers = Partial<Record<string, (init: never) => Promise<unknown>>>;

/** Every Inbox section other than incidents answers with an empty list unless a handler is given. */
export function stubGet(handlers: Handlers): MockInstance {
  return vi.spyOn(api, "GET").mockImplementation(((path: string, init: never) => {
    const handler = handlers[path];
    if (handler) return handler(init);
    if (path === "/api/projects/{project_id}") return ok(project("prj_1", "Test", null));
    if (path.endsWith("/approvals") || path.endsWith("/reviews") || path.endsWith("/activity")) {
      return ok([]);
    }
    return Promise.reject(new Error(`Unexpected GET ${path}`));
  }) as never);
}

export function stubPost(handlers: Handlers): MockInstance {
  return vi.spyOn(api, "POST").mockImplementation(((path: string, init: never) => {
    const handler = handlers[path];
    return handler ? handler(init) : Promise.reject(new Error(`Unexpected POST ${path}`));
  }) as never);
}

export function pageOf(rows: IncidentFixture[], truncated = false) {
  return { rows, total: rows.length, truncated };
}

/** Opens the stream stub every page test needs; returns the fake so a test can emit events. */
export function stubStream(): TestFakeStream {
  const fake = new TestFakeStream();
  vi.spyOn(streamModule, "openStream").mockImplementation(() => {
    setTimeout(() => {
      fake.emitOpen();
    }, 0);
    return fake;
  });
  stubProjects([project("prj_1", "Test", null)]);
  return fake;
}

export function emitEvent(fake: TestFakeStream, event: EventFixture): void {
  fake.emit(event.type, JSON.stringify(event), event.seq);
}

export function bodyOf(spy: MockInstance, path: string): unknown {
  const call = spy.mock.calls.find((c) => c[0] === path);
  return (call?.[1] as { body?: unknown } | undefined)?.body;
}

export function postCount(spy: MockInstance, path: string): number {
  return spy.mock.calls.filter((c) => c[0] === path).length;
}
