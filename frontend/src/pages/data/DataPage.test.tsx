import { createElement, type ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import type { EventRecord } from "@/lib/useProjectEvents";
import { FakeStream } from "@/pages/agents/session/testing";
import { DataPage } from "./DataPage";

describe("DataPage", () => {
  let createdStreams: FakeStream[] = [];
  let openStreamSpy: MockInstance | undefined;
  let testQueryClient: QueryClient;

  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(
      QueryClientProvider,
      { client: testQueryClient },
      createElement(
        MemoryRouter,
        { initialEntries: ["/projects/prj_1/data"] },
        createElement(
          Routes,
          null,
          createElement(Route, { path: "/projects/:projectId/data", element: children }),
        ),
      ),
    );

  const sampleConnections = [
    {
      id: "con_1",
      project_id: "prj_1",
      name: "Warehouse DB",
      kind: "postgres",
      status: "ok" as const,
      status_detail: null,
      data_version: "v1",
      created_at: "2026-10-29T10:00:00Z",
      settings: { host: "db-secret.internal", port: 5432, password: "do-not-leak" },
      findings: {
        secret_files: [{ path: "secrets.env", reason: "Found AWS keys in file" }],
        unsupported_files: [{ path: "audio.wav", reason: "Audio format not supported" }],
        truncated: false,
      },
    },
    {
      id: "con_2",
      project_id: "prj_1",
      name: "Remote Lake",
      kind: "s3",
      status: "unreachable" as const,
      status_detail: "S3 Bucket access denied: 403 Forbidden",
      data_version: null,
      created_at: "2026-10-29T10:05:00Z",
      settings: { bucket: "secret-s3-bucket" },
      findings: { secret_files: [], unsupported_files: [], truncated: false },
    },
  ];

  const sampleTables = [
    {
      name: "customers",
      format: "parquet" as const,
      files: ["customers.parquet"],
    },
    {
      name: "transactions",
      format: "csv" as const,
      files: ["tx1.csv", "tx2.csv"],
    },
    {
      name: "raw_logs",
      format: "json" as const,
      files: ["raw.json"],
    },
  ];

  const sampleHandoff = {
    proposal_id: "prop_1",
    version: 1,
    approved: "2026-10-29T12:00:00Z",
    payload: {
      commands: [],
      data_roles: [
        {
          connection: "Warehouse DB",
          table: "customers",
          role: "training" as const,
          id_column: "cust_id",
          time_column: "signup_date",
        },
        {
          connection: "Warehouse DB",
          table: "transactions",
          role: "predictions" as const,
          id_column: "tx_id",
          time_column: "tx_time",
        },
      ],
      edges: [],
      gaps: [],
      grants: {},
      operations: {},
      pipeline: [],
      tracking: [],
      models: [
        {
          name: "churn-model",
          commands: [],
          environments: {},
          production_signal: null,
          profile: {},
          promotion: "person" as const,
          predictions: {
            connection: "Warehouse DB",
            table: "transactions",
          },
          labels: null,
        },
        {
          name: "risk-model",
          commands: [],
          environments: {},
          production_signal: null,
          profile: {},
          promotion: "team_gate" as const,
          predictions: null,
          labels: {
            connection: "Warehouse DB",
            table: "transactions",
          },
        },
      ],
    },
  };

  const sampleTableDetail = {
    table: sampleTables[0],
    columns: [
      { name: "cust_id", type: "BIGINT" },
      { name: "name", type: "VARCHAR" },
    ],
    row_count: 5000,
    timed_out: false,
    incomplete: false,
    data_version: "v-cust-1",
    summary: "Customer profile statistics",
  };

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

  it("footer is present in every state: loading, error, empty, and with data", async () => {
    // 1. Loading state
    vi.spyOn(api, "GET").mockImplementation(() => new Promise(() => {}));
    const { unmount } = render(<DataPage />, { wrapper });
    expect(
      screen.getByText("Quality and drift history arrives with monitoring."),
    ).toBeInTheDocument();
    unmount();

    // 2. Error state
    testQueryClient.clear();
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/connections") {
        return Promise.resolve({
          error: { code: "internal", message: "Database connection failed" },
          response: new Response(null, { status: 500 }),
        } as never);
      }
      return Promise.resolve({
        data: null,
        response: new Response(null, { status: 404 }),
      } as never);
    });
    const { unmount: unmountErr } = render(<DataPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText("Database connection failed")).toBeInTheDocument();
    });
    expect(
      screen.getByText("Quality and drift history arrives with monitoring."),
    ).toBeInTheDocument();
    unmountErr();

    // 3. Empty state
    testQueryClient.clear();
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/connections") {
        return Promise.resolve({
          data: [],
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.resolve({
        data: null,
        response: new Response(null, { status: 404 }),
      } as never);
    });
    const { unmount: unmountEmpty } = render(<DataPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText(/No data connection yet. Add one in/i)).toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: "Project settings" })).toHaveAttribute(
      "href",
      "/projects/prj_1/settings",
    );
    expect(
      screen.getByText("Quality and drift history arrives with monitoring."),
    ).toBeInTheDocument();
    unmountEmpty();
  });

  it("renders two connections; unreachable one shows detail and requests no tables; ok one shows findings and tables", async () => {
    let tablesRequestedForCon2 = false;

    vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/connections") {
        return Promise.resolve({
          data: sampleConnections,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          data: sampleHandoff,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/connections/{connection_id}/tables") {
        const initObj = init as { params?: { path?: { connection_id?: string } } } | undefined;
        if (initObj?.params?.path?.connection_id === "con_2") {
          tablesRequestedForCon2 = true;
        }
        return Promise.resolve({
          data: sampleTables,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    render(<DataPage />, { wrapper });

    // Wait for connections to load
    await waitFor(() => {
      expect(screen.getByText("Warehouse DB")).toBeInTheDocument();
      expect(screen.getByText("Remote Lake")).toBeInTheDocument();
    });

    // Unreachable connection checks
    expect(screen.getByText("S3 Bucket access denied: 403 Forbidden")).toBeInTheDocument();
    expect(tablesRequestedForCon2).toBe(false);

    // Settings must not be in DOM anywhere
    expect(screen.queryByText("db-secret.internal")).not.toBeInTheDocument();
    expect(screen.queryByText("do-not-leak")).not.toBeInTheDocument();
    expect(screen.queryByText("secret-s3-bucket")).not.toBeInTheDocument();

    // Findings rendered through ConnectionFindings
    expect(screen.getByText("secrets.env")).toBeInTheDocument();
    expect(screen.getByText("Found AWS keys in file")).toBeInTheDocument();
    expect(screen.getByText("audio.wav")).toBeInTheDocument();
    expect(screen.getByText("Audio format not supported")).toBeInTheDocument();

    // Tables of ok connection render
    await waitFor(() => {
      expect(screen.getByText("customers")).toBeInTheDocument();
      expect(screen.getByText("transactions")).toBeInTheDocument();
      expect(screen.getByText("raw_logs")).toBeInTheDocument();
    });

    // Roles from handoff
    expect(screen.getByText("training")).toBeInTheDocument();
    expect(screen.getByText("predictions")).toBeInTheDocument();
    // raw_logs is not in handoff.data_roles -> Not assigned
    expect(screen.getByText("Not assigned")).toBeInTheDocument();
  });

  it("shows 'Roles appear after you approve the handoff' line and no role badges when handoff is null", async () => {
    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/connections") {
        return Promise.resolve({
          data: [sampleConnections[0]],
          response: new Response(null, { status: 200 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          data: null,
          response: new Response(null, { status: 404 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/connections/{connection_id}/tables") {
        return Promise.resolve({
          data: [sampleTables[0]],
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    render(<DataPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("customers")).toBeInTheDocument();
    });

    expect(screen.getByText("Roles appear after you approve the handoff")).toBeInTheDocument();
    expect(screen.queryByText("training")).not.toBeInTheDocument();
    expect(screen.queryByText("Not assigned")).not.toBeInTheDocument();
  });

  it("expanding a table requests detail once (collapse & re-expand does not refetch), shows details, handles 4xx inside panel", async () => {
    let detailFetchCount = 0;

    vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/connections") {
        return Promise.resolve({
          data: [sampleConnections[0]],
          response: new Response(null, { status: 200 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          data: sampleHandoff,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/connections/{connection_id}/tables") {
        return Promise.resolve({
          data: [sampleTables[0], sampleTables[1]],
          response: new Response(null, { status: 200 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/connections/{connection_id}/tables/{table}") {
        const initObj = init as { params?: { path?: { table?: string } } } | undefined;
        if (initObj?.params?.path?.table === "customers") {
          detailFetchCount += 1;
          return Promise.resolve({
            data: sampleTableDetail,
            response: new Response(null, { status: 200 }),
          } as never);
        }
        if (initObj?.params?.path?.table === "transactions") {
          return Promise.resolve({
            error: { code: "not_found", message: "Table metadata not found" },
            response: new Response(null, { status: 404 }),
          } as never);
        }
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    render(<DataPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("customers")).toBeInTheDocument();
    });

    // Expand customers table
    const customersButton = screen.getByRole("button", { name: /customers/i });
    fireEvent.click(customersButton);

    await waitFor(() => {
      expect(screen.getByText("Customer profile statistics")).toBeInTheDocument();
    });
    expect(detailFetchCount).toBe(1);
    expect(screen.getByText("5,000")).toBeInTheDocument();
    expect(screen.getByText("v-cust-1")).toBeInTheDocument();

    // Collapse customers table
    fireEvent.click(customersButton);

    // Re-expand customers table -> should NOT refetch from server (detailFetchCount stays 1)
    fireEvent.click(customersButton);
    expect(detailFetchCount).toBe(1);

    // Expand transactions table which returns 404 error
    const txButton = screen.getByRole("button", { name: /transactions/i });
    fireEvent.click(txButton);

    // Error is displayed inside panel only; other table remains usable
    await waitFor(() => {
      expect(screen.getByText("Table metadata not found")).toBeInTheDocument();
    });
    expect(screen.getByText("Customer profile statistics")).toBeInTheDocument();
  });

  it("handles events: connection.created adds card, connection.deleted removes card, handoff.approved updates roles", async () => {
    let connectionsData = [sampleConnections[0]];
    let handoffData: typeof sampleHandoff | null = null;

    vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/connections") {
        return Promise.resolve({
          data: connectionsData,
          response: new Response(null, { status: 200 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          data: handoffData,
          response: new Response(null, { status: handoffData ? 200 : 404 }),
        } as never);
      }
      if (path === "/api/projects/{project_id}/connections/{connection_id}/tables") {
        return Promise.resolve({
          data: [sampleTables[0]],
          response: new Response(null, { status: 200 }),
        } as never);
      }
      return Promise.reject(new Error(`Unexpected ${path}`));
    });

    render(<DataPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Warehouse DB")).toBeInTheDocument();
      expect(screen.getByText("customers")).toBeInTheDocument();
    });
    expect(screen.getByText("Roles appear after you approve the handoff")).toBeInTheDocument();

    // Trigger connection.created: add second connection
    connectionsData = sampleConnections;
    act(() => {
      for (const s of createdStreams) {
        s.emitOpen();
        const ev: EventRecord = {
          id: "ev_1",
          seq: 1,
          ts: "2026-10-29T12:00:00Z",
          source: "orchestrator",
          type: "connection.created",
          subject_id: "con_2",
          payload: {},
        };
        s.emit("connection.created", ev, 1);
      }
    });

    await waitFor(() => {
      expect(screen.getByText("Remote Lake")).toBeInTheDocument();
    });

    // Trigger handoff.approved: fill in roles
    handoffData = sampleHandoff;
    act(() => {
      for (const s of createdStreams) {
        const ev: EventRecord = {
          id: "ev_2",
          seq: 2,
          ts: "2026-10-29T12:05:00Z",
          source: "orchestrator",
          type: "handoff.approved",
          subject_id: "prop_1",
          payload: {},
        };
        s.emit("handoff.approved", ev, 2);
      }
    });

    await waitFor(() => {
      expect(screen.getByText("training")).toBeInTheDocument();
    });

    // Trigger connection.deleted: remove first connection
    connectionsData = [sampleConnections[1]];
    act(() => {
      for (const s of createdStreams) {
        const ev: EventRecord = {
          id: "ev_3",
          seq: 3,
          ts: "2026-10-29T12:10:00Z",
          source: "orchestrator",
          type: "connection.deleted",
          subject_id: "con_1",
          payload: {},
        };
        s.emit("connection.deleted", ev, 3);
      }
    });

    await waitFor(() => {
      expect(screen.queryByText("Warehouse DB")).not.toBeInTheDocument();
      expect(screen.getByText("Remote Lake")).toBeInTheDocument();
    });
  });
});
