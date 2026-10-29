import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as streamModule from "@/lib/stream";
import type { ConnectionView } from "@/lib/useConnections";
import * as connectionsModule from "@/lib/useConnections";
import type { HandoffView } from "@/lib/useHandoff";
import { FakeStream } from "@/pages/agents/session/testing";
import { ConnectionCard } from "./ConnectionCard";

describe("ConnectionCard", () => {
  let testQueryClient: QueryClient;

  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: testQueryClient }, children);

  const okConnection: ConnectionView = {
    id: "con_ok",
    name: "Production DB",
    kind: "postgres",
    status: "ok",
    status_detail: null,
    data_version: "v1",
    settings: {
      host: "secret-db.internal",
      port: 5432,
      database: "prod_db",
      password: "supersecretpassword",
    },
    findings: {
      secret_files: [],
      unsupported_files: [],
      truncated: false,
    },
  };

  const unreachableConnection: ConnectionView = {
    id: "con_unreachable",
    name: "Offline Warehouse",
    kind: "snowflake",
    status: "unreachable",
    status_detail: "Connection refused: host warehouse.internal is down",
    data_version: null,
    settings: {
      account: "secret-account",
      warehouse: "wh_1",
    },
    findings: {
      secret_files: [],
      unsupported_files: [],
      truncated: false,
    },
  };

  const connectionWithFindings: ConnectionView = {
    id: "con_dirty",
    name: "Dirty S3 Bucket",
    kind: "s3",
    status: "ok",
    status_detail: null,
    data_version: "v1",
    settings: {
      bucket: "my-bucket",
    },
    findings: {
      secret_files: [
        {
          path: ".env.production",
          reason: "Matches known secret pattern: .env*",
        },
      ],
      unsupported_files: [
        {
          path: "model.bin",
          reason: "Binary format .bin is not a supported tabular format",
        },
      ],
      truncated: false,
    },
  };

  const sampleHandoff: HandoffView = {
    proposal_id: "prop_1",
    version: 1,
    approved: "2026-10-29T12:00:00Z",
    payload: {
      commands: [],
      data_roles: [],
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
    vi.spyOn(streamModule, "openStream").mockImplementation((url: string) => new FakeStream(url));
  });

  it("renders ok connection with kind, status, tables, and renders NO settings values", () => {
    vi.spyOn(connectionsModule, "useTables").mockReturnValue({
      data: [
        { name: "users", format: "parquet", files: ["users.parquet"] },
        { name: "orders", format: "csv", files: ["orders.csv"] },
      ],
      isLoading: false,
      error: null,
    } as never);

    render(<ConnectionCard projectId="prj_1" connection={okConnection} handoff={sampleHandoff} />, {
      wrapper,
    });

    expect(screen.getByText("Production DB")).toBeInTheDocument();
    expect(screen.getByText("postgres")).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByText("users")).toBeInTheDocument();
    expect(screen.getByText("orders")).toBeInTheDocument();

    // Verify NO settings are rendered in the DOM
    expect(screen.queryByText("secret-db.internal")).not.toBeInTheDocument();
    expect(screen.queryByText("5432")).not.toBeInTheDocument();
    expect(screen.queryByText("prod_db")).not.toBeInTheDocument();
    expect(screen.queryByText("supersecretpassword")).not.toBeInTheDocument();
  });

  it("unreachable connection shows status detail and NEVER requests tables", () => {
    const useTablesSpy = vi.spyOn(connectionsModule, "useTables");

    render(
      <ConnectionCard
        projectId="prj_1"
        connection={unreachableConnection}
        handoff={sampleHandoff}
      />,
      { wrapper },
    );

    expect(screen.getByText("Offline Warehouse")).toBeInTheDocument();
    expect(screen.getByText("snowflake")).toBeInTheDocument();
    expect(screen.getByText("unreachable")).toBeInTheDocument();
    expect(
      screen.getByText("Connection refused: host warehouse.internal is down"),
    ).toBeInTheDocument();

    // Verify useTables was NOT called with connection.id (or called with undefined)
    if (useTablesSpy.mock.calls.length > 0) {
      for (const call of useTablesSpy.mock.calls) {
        // Either connectionId must be undefined or not called
        expect(call[1]).toBeUndefined();
      }
    }

    // Settings must not be present
    expect(screen.queryByText("secret-account")).not.toBeInTheDocument();
  });

  it("renders secret-like and unsupported files through ConnectionFindings", () => {
    vi.spyOn(connectionsModule, "useTables").mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    } as never);

    render(
      <ConnectionCard
        projectId="prj_1"
        connection={connectionWithFindings}
        handoff={sampleHandoff}
      />,
      { wrapper },
    );

    expect(screen.getByText(".env.production")).toBeInTheDocument();
    expect(screen.getByText("Matches known secret pattern: .env*")).toBeInTheDocument();
    expect(screen.getByText("model.bin")).toBeInTheDocument();
    expect(
      screen.getByText("Binary format .bin is not a supported tabular format"),
    ).toBeInTheDocument();
  });
});
