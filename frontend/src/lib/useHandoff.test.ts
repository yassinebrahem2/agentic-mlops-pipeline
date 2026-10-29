import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import type { EventRecord } from "@/lib/useProjectEvents";
import { FakeStream } from "@/pages/agents/session/testing";
import { handoffKey, useHandoff } from "./useHandoff";

describe("useHandoff", () => {
  let createdStreams: FakeStream[] = [];
  let openStreamSpy: MockInstance | undefined;
  let testQueryClient: QueryClient;

  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: testQueryClient }, children);

  beforeEach(() => {
    testQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    createdStreams = [];
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockImplementation((url: string) => {
      const stream = new FakeStream(url);
      createdStreams.push(stream);
      return stream;
    });
  });

  afterEach(() => {
    openStreamSpy?.mockRestore();
    vi.restoreAllMocks();
  });

  it("builds query key", () => {
    expect(handoffKey("prj_1")).toEqual(["projects", "prj_1", "handoff"]);
  });

  it("returns handoff view when present", async () => {
    const mockHandoff = {
      project_id: "prj_1",
      proposal_id: "prp_1",
      approved_at: "2026-10-29T10:00:00Z",
      payload: {
        problem_type: "classification",
        data_roles: [],
        models: [],
      },
    };

    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          data: mockHandoff,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useHandoff("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockHandoff);
  });

  it("returns null for a null body", async () => {
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          data: null,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useHandoff("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toBeNull();
  });

  it("returns null for a 404 response", async () => {
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          error: { code: "not_found", message: "No handoff" },
          response: new Response(null, { status: 404 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useHandoff("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toBeNull();
  });

  it("surfaces a 500 error", async () => {
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          error: { code: "internal_error", message: "Internal server error" },
          response: new Response(null, { status: 500 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useHandoff("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toBe("Internal server error");
  });

  it("handoff.approved event refetches handoff", async () => {
    let callCount = 0;
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/handoff") {
        callCount += 1;
        return Promise.resolve({
          data: null,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useHandoff("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(callCount).toBe(1);

    expect(createdStreams).toHaveLength(1);
    act(() => {
      createdStreams[0]?.emitOpen();
      const mockEvent: EventRecord = {
        id: "ev_1",
        seq: 1,
        ts: "2026-10-29T12:00:00Z",
        source: "orchestrator",
        type: "handoff.approved",
        subject_id: "prp_1",
        payload: {},
      };
      createdStreams[0]?.emit("handoff.approved", mockEvent, 1);
    });

    await waitFor(
      () => {
        expect(callCount).toBe(2);
      },
      { timeout: 2000 },
    );
  });
});
