import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ConnectionView, TableRef } from "@/lib/useConnections";
import * as connectionsModule from "@/lib/useConnections";
import type { HandoffView } from "@/lib/useHandoff";
import { TableRow } from "./TableRow";

describe("TableRow", () => {
  let testQueryClient: QueryClient;

  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: testQueryClient }, children);

  const sampleConnection: ConnectionView = {
    id: "con_1",
    name: "warehouse",
    kind: "postgres",
    status: "ok",
    status_detail: null,
    data_version: "v1",
    settings: { host: "db.internal" },
    findings: { secret_files: [], unsupported_files: [], truncated: false },
  };

  const sampleTable: TableRef = {
    name: "transactions",
    format: "parquet",
    files: ["tx_01.parquet", "tx_02.parquet"],
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
          table: "transactions",
          role: "training",
          id_column: "tx_id",
          time_column: "created_at",
        },
      ],
      edges: [],
      gaps: [],
      grants: {},
      operations: {},
      pipeline: [],
      tracking: [],
      models: [],
    },
  } as unknown as HandoffView;

  beforeEach(() => {
    testQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.restoreAllMocks();
  });

  it("renders 'Roles appear after you approve the handoff' and no role badges when handoff is null", () => {
    render(
      <TableRow
        projectId="prj_1"
        connection={sampleConnection}
        table={sampleTable}
        handoff={null}
      />,
      { wrapper },
    );

    expect(screen.getByText("transactions")).toBeInTheDocument();
    expect(screen.getByText("parquet")).toBeInTheDocument();
    expect(screen.getByText("2 files")).toBeInTheDocument();
    expect(screen.getByText("Roles appear after you approve the handoff")).toBeInTheDocument();
    expect(screen.queryByText("training")).not.toBeInTheDocument();
    expect(screen.queryByText("Not assigned")).not.toBeInTheDocument();
  });

  it("renders role badge when role matches in handoff", () => {
    render(
      <TableRow
        projectId="prj_1"
        connection={sampleConnection}
        table={sampleTable}
        handoff={sampleHandoff}
      />,
      { wrapper },
    );

    expect(screen.getByText("training")).toBeInTheDocument();
    expect(
      screen.queryByText("Roles appear after you approve the handoff"),
    ).not.toBeInTheDocument();
  });

  it("renders 'Not assigned' when handoff exists but table is not in data_roles", () => {
    const tableWithoutRole: TableRef = {
      name: "logs",
      format: "json",
      files: ["log1.json"],
    };

    render(
      <TableRow
        projectId="prj_1"
        connection={sampleConnection}
        table={tableWithoutRole}
        handoff={sampleHandoff}
      />,
      { wrapper },
    );

    expect(screen.getByText("1 file")).toBeInTheDocument();
    expect(screen.getByText("Not assigned")).toBeInTheDocument();
  });

  it("expands on click and renders TableDetailPanel", () => {
    vi.spyOn(connectionsModule, "useTableDetail").mockReturnValue({
      data: {
        table: sampleTable,
        columns: [{ name: "tx_id", type: "BIGINT" }],
        row_count: 500,
        timed_out: false,
        incomplete: false,
        data_version: "v1",
        summary: "Summary text",
      },
      isLoading: false,
      error: null,
    } as never);

    render(
      <TableRow
        projectId="prj_1"
        connection={sampleConnection}
        table={sampleTable}
        handoff={sampleHandoff}
      />,
      { wrapper },
    );

    // Click trigger to expand
    const trigger = screen.getByRole("button", { name: /transactions/i });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    // Detail panel should be rendered
    expect(screen.getByText("Summary text")).toBeInTheDocument();
    expect(screen.getByText("500")).toBeInTheDocument();
  });
});
