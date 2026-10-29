import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import type { EventRecord } from "@/lib/useProjectEvents";
import { FakeStream } from "@/pages/agents/session/testing";
import { connectionsKey, tablesKey, useConnections, useTables } from "./useConnections";

describe("useConnections", () => {
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

  it("builds query keys", () => {
    expect(connectionsKey("prj_1")).toEqual(["projects", "prj_1", "connections"]);
    expect(tablesKey("prj_1", "con_1")).toEqual([
      "projects",
      "prj_1",
      "connections",
      "con_1",
      "tables",
    ]);
  });

  it("useConnections calls /connections route and returns connections list", async () => {
    const mockConnections = [
      {
        id: "con_1",
        project_id: "prj_1",
        name: "Warehouse",
        kind: "postgres",
        status: "ok" as const,
        status_detail: null,
        created_at: "2026-10-29T10:00:00Z",
        findings: [],
      },
    ];

    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/connections") {
        return Promise.resolve({
          data: mockConnections,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useConnections("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockConnections);
  });

  it("useTables calls /connections/{connection_id}/tables route and returns tables list", async () => {
    const mockTables = [
      {
        table: "customers",
        format: "parquet",
        file_count: 5,
      },
    ];

    vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/connections/{connection_id}/tables") {
        const initObj = init as
          { params?: { path?: { project_id?: string; connection_id?: string } } } | undefined;
        const pathParams = initObj?.params?.path;
        expect(pathParams).toEqual({ project_id: "prj_1", connection_id: "con_1" });
        return Promise.resolve({
          data: mockTables,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useTables("prj_1", "con_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockTables);
  });

  it("connection events refetch connections", async () => {
    let callCount = 0;
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/connections") {
        callCount += 1;
        return Promise.resolve({
          data: [],
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useConnections("prj_1"), { wrapper });

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
        type: "connection.created",
        subject_id: "con_1",
        payload: {},
      };
      createdStreams[0]?.emit("connection.created", mockEvent, 1);
    });

    await waitFor(
      () => {
        expect(callCount).toBe(2);
      },
      { timeout: 2000 },
    );
  });
});
