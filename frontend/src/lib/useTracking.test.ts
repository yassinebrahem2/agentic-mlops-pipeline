import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import type { EventRecord } from "@/lib/useProjectEvents";
import { FakeStream } from "@/pages/agents/session/testing";
import {
  RUNS_PAGE_LIMIT,
  trackingKey,
  useCiRuns,
  useCreateSource,
  useDeployments,
  useModelVersions,
  useRunByKey,
  useRunMetrics,
  useSetSourceCredential,
  useSyncNow,
  useTrackedModels,
  useTrackedRuns,
  useTrackingSources,
  VERSIONS_PAGE_LIMIT,
} from "./useTracking";

describe("useTracking", () => {
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

  it("exports page limits and builds trackingKey", () => {
    expect(RUNS_PAGE_LIMIT).toBe(200);
    expect(VERSIONS_PAGE_LIMIT).toBe(100);
    expect(trackingKey("prj_1", "sources")).toEqual(["projects", "prj_1", "tracking", "sources"]);
    expect(trackingKey("prj_1", "runs")).toEqual(["projects", "prj_1", "tracking", "runs"]);
  });

  it("useTrackingSources calls sources route and returns source views", async () => {
    const mockSources = [
      {
        id: "trk_1",
        project_id: "prj_1",
        type: "mlflow" as const,
        name: "MLflow",
        auth: "credential" as const,
        credential_set: true,
        created_at: "2026-10-29T10:00:00Z",
        last_synced_at: null,
        last_error: null,
      },
    ];

    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.resolve({
          data: mockSources,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useTrackingSources("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockSources);
  });

  it("useTrackedModels calls models route and returns stubbed page", async () => {
    const mockPage = {
      rows: [
        {
          source_id: "trk_1",
          name: "fraud-detector",
          versions: 3,
          production_version: "2",
          last_updated: "2026-10-29T11:00:00Z",
        },
      ],
      total: 1,
      truncated: false,
    };

    let calledQuery: unknown = null;
    vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/tracking/models") {
        calledQuery = (init as { params?: { query?: unknown } } | undefined)?.params?.query;
        return Promise.resolve({
          data: mockPage,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useTrackedModels("prj_1", { limit: 50, offset: 10 }), {
      wrapper,
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockPage);
    expect(calledQuery).toEqual({ limit: 50, offset: 10 });
  });

  it("useModelVersions calls versions route with sourceId and model filters", async () => {
    const mockPage = {
      rows: [
        {
          version: "1",
          source_id: "trk_1",
          model: "fraud-detector",
          aliases: ["production"],
          created_at: "2026-10-29T10:00:00Z",
          run_key: "run_123",
          run_url: null,
          description: null,
        },
      ],
      total: 1,
      truncated: false,
    };

    let calledQuery: unknown = null;
    vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/tracking/versions") {
        calledQuery = (init as { params?: { query?: unknown } } | undefined)?.params?.query;
        return Promise.resolve({
          data: mockPage,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(
      () => useModelVersions("prj_1", "trk_1", "fraud-detector", { limit: 100 }),
      { wrapper },
    );

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockPage);
    expect(calledQuery).toEqual({
      source_id: "trk_1",
      model: "fraud-detector",
      limit: 100,
      offset: undefined,
    });
  });

  it("useTrackedRuns calls runs route and returns stubbed page", async () => {
    const mockPage = {
      rows: [
        {
          run_key: "run_1",
          source_id: "trk_1",
          experiment: "fraud-v1",
          name: "exp-run",
          status: "succeeded" as const,
          start_time: "2026-10-29T08:00:00Z",
          end_time: "2026-10-29T08:15:00Z",
          git_commit: "abcdef123456",
          datasets: ["train.csv"],
          params: { lr: "0.01" },
          tags: {},
        },
      ],
      total: 1,
      truncated: false,
    };

    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/tracking/runs") {
        return Promise.resolve({
          data: mockPage,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useTrackedRuns("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockPage);
  });

  it("useRunByKey calls runs route with run_key parameter and returns the first row or null", async () => {
    const mockRun = {
      run_key: "run_target",
      source_id: "trk_1",
      experiment: "fraud-v1",
      name: "target-run",
      status: "succeeded" as const,
      start_time: "2026-10-29T08:00:00Z",
      end_time: "2026-10-29T08:15:00Z",
      git_commit: "abcdef1",
      datasets: [],
      params: {},
      tags: {},
    };

    let calledQuery: unknown = null;
    vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/tracking/runs") {
        calledQuery = (init as { params?: { query?: unknown } } | undefined)?.params?.query;
        return Promise.resolve({
          data: { rows: [mockRun], total: 1, truncated: false },
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useRunByKey("prj_1", "run_target", true), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockRun);
    expect(calledQuery).toEqual({ run_key: "run_target" });
  });

  it("useCiRuns calls ci-runs route and returns stubbed page", async () => {
    const mockPage = {
      rows: [
        {
          ci_run_key: "ci_1",
          source_id: "trk_2",
          workflow: "ci.yml",
          status: "succeeded" as const,
          start_time: "2026-10-29T09:00:00Z",
          duration_s: 45,
          git_commit: "abcdef1",
          url: "https://github.com/org/repo/actions/runs/1",
        },
      ],
      total: 1,
      truncated: false,
    };

    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({
          data: mockPage,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useCiRuns("prj_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockPage);
  });

  it("useDeployments calls deployments route with filters", async () => {
    const mockPage = {
      rows: [
        {
          deployment_id: "dep_1",
          source_id: "trk_1",
          model: "fraud-detector",
          alias: "production",
          to_version: "2",
          seen_at: "2026-10-29T10:00:00Z",
        },
      ],
      total: 1,
      truncated: false,
    };

    let calledQuery: unknown = null;
    vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/tracking/deployments") {
        calledQuery = (init as { params?: { query?: unknown } } | undefined)?.params?.query;
        return Promise.resolve({
          data: mockPage,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(
      () => useDeployments("prj_1", "trk_1", "fraud-detector", { limit: 10 }),
      { wrapper },
    );

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockPage);
    expect(calledQuery).toEqual({
      source_id: "trk_1",
      model: "fraud-detector",
      limit: 10,
      offset: undefined,
    });
  });

  it("useRunMetrics calls metrics route with run_key and latest=true", async () => {
    const mockPage = {
      rows: [
        {
          run_key: "run_1",
          metric: "accuracy",
          step: 10,
          value: 0.95,
          timestamp: "2026-10-29T08:10:00Z",
        },
      ],
      total: 1,
      truncated: false,
    };

    let calledQuery: unknown = null;
    vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/tracking/metrics") {
        calledQuery = (init as { params?: { query?: unknown } } | undefined)?.params?.query;
        return Promise.resolve({
          data: mockPage,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useRunMetrics("prj_1", "run_1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(mockPage);
    expect(calledQuery).toEqual({
      run_key: "run_1",
      latest: true,
      limit: undefined,
      offset: undefined,
    });
  });

  it("tracking.synced event refetches a tracking hook", async () => {
    let callCount = 0;
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/tracking/sources") {
        callCount += 1;
        return Promise.resolve({
          data: [
            {
              id: "trk_1",
              project_id: "prj_1",
              type: "mlflow",
              name: `MLflow count ${String(callCount)}`,
              auth: "none",
              credential_set: false,
              created_at: "2026-10-29T10:00:00Z",
              last_synced_at: null,
              last_error: null,
            },
          ],
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useTrackingSources("prj_1"), { wrapper });

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
        type: "tracking.synced",
        subject_id: "trk_1",
        payload: {},
      };
      createdStreams[0]?.emit("tracking.synced", mockEvent, 1);
    });

    // Wait for debounced invalidation and refetch
    await waitFor(
      () => {
        expect(callCount).toBe(2);
      },
      { timeout: 2000 },
    );
  });

  it("useSyncNow posts to /tracking/sync without source_id", async () => {
    let calledBody: unknown = "not-called";
    vi.spyOn(api, "POST").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/tracking/sync") {
        calledBody = (init as { body?: unknown } | undefined)?.body;
        return Promise.resolve({
          data: undefined,
          response: new Response(null, { status: 202 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useSyncNow("prj_1"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(calledBody).toBeUndefined();
  });

  it("useCreateSource posts type, settings, auth and refreshes sources", async () => {
    let calledBody: unknown = null;
    const invalidateSpy = vi.spyOn(testQueryClient, "invalidateQueries");

    vi.spyOn(api, "POST").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/tracking/sources") {
        calledBody = (init as { body?: unknown } | undefined)?.body;
        return Promise.resolve({
          data: {
            id: "trk_new",
            project_id: "prj_1",
            type: "mlflow",
            name: "MLflow New",
            auth: "none",
            credential_set: false,
            created_at: "2026-10-29T10:00:00Z",
            last_synced_at: null,
            last_error: null,
          },
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useCreateSource("prj_1"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        type: "mlflow",
        settings: { tracking_uri: "http://localhost:5000" },
        auth: "none",
      });
    });

    expect(calledBody).toEqual({
      type: "mlflow",
      settings: { tracking_uri: "http://localhost:5000" },
      auth: "none",
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: trackingKey("prj_1", "sources") });
  });

  it("useSetSourceCredential puts to credential route only, value never appears in query key, cache or error text", async () => {
    let calledPath = "";
    let calledBody: unknown = null;
    const secretValue = "super-secret-token-12345";
    const invalidateSpy = vi.spyOn(testQueryClient, "invalidateQueries");

    vi.spyOn(api, "PUT").mockImplementation((path: string, init?: unknown) => {
      calledPath = path;
      calledBody = (init as { body?: unknown } | undefined)?.body;
      if (path === "/api/projects/{project_id}/tracking/sources/{source_id}/credential") {
        return Promise.resolve({
          data: {
            id: "trk_1",
            project_id: "prj_1",
            type: "mlflow",
            name: "MLflow",
            auth: "credential",
            credential_set: true,
            created_at: "2026-10-29T10:00:00Z",
            last_synced_at: null,
            last_error: null,
          },
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    const { result } = renderHook(() => useSetSourceCredential("prj_1"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ sourceId: "trk_1", credential: secretValue });
    });

    expect(calledPath).toBe("/api/projects/{project_id}/tracking/sources/{source_id}/credential");
    expect(calledBody).toEqual({ credential: secretValue });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: trackingKey("prj_1", "sources") });

    // Assert secret is not in cache, query keys, or mutation cache variables
    const cacheEntries = JSON.stringify(testQueryClient.getQueryCache().getAll());
    expect(cacheEntries).not.toContain(secretValue);
    for (const mutation of testQueryClient.getMutationCache().getAll()) {
      const vars = mutation.state.variables as Record<string, unknown> | undefined;
      expect(vars?.credential).toBeUndefined();
    }

    // Test error case: error text must not contain secret
    vi.spyOn(api, "PUT").mockImplementation(() => {
      return Promise.resolve({
        error: { code: "invalid_credential", message: `Failed with token ${secretValue}` },
        response: new Response(null, { status: 400 }),
      } as never);
    });

    await expect(
      result.current.mutateAsync({ sourceId: "trk_1", credential: secretValue }),
    ).rejects.toThrow();

    try {
      await result.current.mutateAsync({ sourceId: "trk_1", credential: secretValue });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      expect(msg).not.toContain(secretValue);
    }

    for (const mutation of testQueryClient.getMutationCache().getAll()) {
      const vars = mutation.state.variables as Record<string, unknown> | undefined;
      expect(vars?.credential).toBeUndefined();
    }
  });
});
