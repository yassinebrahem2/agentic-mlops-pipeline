import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { TableRef } from "@/lib/useConnections";
import * as connectionsModule from "@/lib/useConnections";
import type { HandoffView } from "@/lib/useHandoff";
import { TableDetailPanel } from "./TableDetailPanel";

describe("TableDetailPanel", () => {
  let testQueryClient: QueryClient;

  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: testQueryClient }, children);

  const sampleTable: TableRef = {
    name: "events",
    format: "parquet",
    files: ["events.parquet"],
  };

  const sampleHandoff: HandoffView = {
    proposal_id: "prop_1",
    version: 1,
    approved: "2026-10-29T12:00:00Z",
    payload: {
      commands: [],
      data_roles: [
        {
          connection: "warehouse",
          table: "events",
          role: "training",
          id_column: "event_id",
          time_column: "timestamp",
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
          name: "event-predictor",
          commands: [],
          environments: {},
          production_signal: null,
          profile: {},
          promotion: "person",
          predictions: {
            connection: "warehouse",
            table: "events",
          },
          labels: null,
        },
      ],
    },
  } as unknown as HandoffView;

  beforeEach(() => {
    testQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.restoreAllMocks();
  });

  it("shows loading indicator when detail is loading", () => {
    vi.spyOn(connectionsModule, "useTableDetail").mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    } as never);

    render(
      <TableDetailPanel
        projectId="prj_1"
        connectionId="con_1"
        connectionName="warehouse"
        table={sampleTable}
        handoff={sampleHandoff}
        isExpanded={true}
      />,
      { wrapper },
    );

    expect(screen.getByText(/loading table details/i)).toBeInTheDocument();
  });

  it("renders error message inside panel when query fails (e.g. 504 profile_timeout)", () => {
    vi.spyOn(connectionsModule, "useTableDetail").mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new ApiError("profile_timeout", "Profile query timed out after 30s", 504),
    } as never);

    render(
      <TableDetailPanel
        projectId="prj_1"
        connectionId="con_1"
        connectionName="warehouse"
        table={sampleTable}
        handoff={sampleHandoff}
        isExpanded={true}
      />,
      { wrapper },
    );

    expect(screen.getByText(/Profile query timed out after 30s/)).toBeInTheDocument();
  });

  it("renders full table detail, columns, summary, role metadata, and models using", () => {
    vi.spyOn(connectionsModule, "useTableDetail").mockReturnValue({
      data: {
        table: sampleTable,
        columns: [
          { name: "event_id", type: "VARCHAR" },
          { name: "timestamp", type: "TIMESTAMP" },
          { name: "value", type: "DOUBLE" },
        ],
        row_count: 1250000,
        timed_out: false,
        incomplete: false,
        data_version: "hash-xyz-123",
        summary: "Column stats:\n- event_id: unique\n- value: mean 42.0",
      },
      isLoading: false,
      error: null,
    } as never);

    render(
      <TableDetailPanel
        projectId="prj_1"
        connectionId="con_1"
        connectionName="warehouse"
        table={sampleTable}
        handoff={sampleHandoff}
        isExpanded={true}
      />,
      { wrapper },
    );

    // Row count formatted
    expect(screen.getByText("1,250,000")).toBeInTheDocument();

    // Data version
    expect(screen.getByText("hash-xyz-123")).toBeInTheDocument();

    // Columns
    expect(screen.getAllByText("event_id")).toHaveLength(2); // once in metadata, once in columns table
    expect(screen.getByText("VARCHAR")).toBeInTheDocument();
    expect(screen.getAllByText("timestamp")).toHaveLength(2); // once in metadata, once in columns table
    expect(screen.getByText("TIMESTAMP")).toBeInTheDocument();

    // Summary inside pre
    const pre = screen.getByText(/Column stats:/);
    expect(pre.tagName.toLowerCase()).toBe("pre");
    expect(pre.textContent).toBe("Column stats:\n- event_id: unique\n- value: mean 42.0");

    // Role metadata
    expect(screen.getByText("ID column")).toBeInTheDocument();
    expect(screen.getByText("Time column")).toBeInTheDocument();

    // Models using
    expect(screen.getByText(/Predictions of event-predictor/)).toBeInTheDocument();
  });

  it("renders 'unknown, timed out' when row_count is null and timed_out is true (Decision #425)", () => {
    vi.spyOn(connectionsModule, "useTableDetail").mockReturnValue({
      data: {
        table: sampleTable,
        columns: [{ name: "id", type: "BIGINT" }],
        row_count: null,
        timed_out: true,
        incomplete: false,
        data_version: "v-timed-out",
        summary: "Summary text",
      },
      isLoading: false,
      error: null,
    } as never);

    render(
      <TableDetailPanel
        projectId="prj_1"
        connectionId="con_1"
        connectionName="warehouse"
        table={sampleTable}
        handoff={null}
        isExpanded={true}
      />,
      { wrapper },
    );

    expect(screen.getByText("unknown, timed out")).toBeInTheDocument();
  });

  it("renders 'incomplete (memory limit)' notice when incomplete is true (Decision #432)", () => {
    vi.spyOn(connectionsModule, "useTableDetail").mockReturnValue({
      data: {
        table: sampleTable,
        columns: [{ name: "id", type: "BIGINT" }],
        row_count: 50000,
        timed_out: false,
        incomplete: true,
        data_version: "v-incomplete",
        summary: "Profile incomplete (memory limit)",
      },
      isLoading: false,
      error: null,
    } as never);

    render(
      <TableDetailPanel
        projectId="prj_1"
        connectionId="con_1"
        connectionName="warehouse"
        table={sampleTable}
        handoff={null}
        isExpanded={true}
      />,
      { wrapper },
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/incomplete \(memory limit\)/i);
  });
});
