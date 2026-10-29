import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { ConnectionsSection } from "./ConnectionsSection";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type ConnectionView = components["schemas"]["ConnectionView"];
type TableRef = components["schemas"]["TableRef"];

const BASE = "/api/projects/prj_demo";

const cleanFindings = {
  secret_files: [],
  unsupported_files: [],
  truncated: false,
};

const okConnection: ConnectionView = {
  id: "con_ok",
  name: "sales-data",
  kind: "local_folder",
  settings: { path: "/data/sales" },
  status: "ok",
  status_detail: null,
  data_version: "v1_abcdef123456",
  findings: cleanFindings,
};

const unreachableConnection: ConnectionView = {
  id: "con_unreach",
  name: "missing-data",
  kind: "local_folder",
  settings: { path: "/data/missing" },
  status: "unreachable",
  status_detail: "Folder does not exist",
  data_version: null,
  findings: cleanFindings,
};

const sampleTables: TableRef[] = [
  { name: "orders", format: "csv", files: ["orders.csv"] },
  { name: "customers", format: "parquet", files: ["customers.parquet"] },
];

let stream: ReturnType<typeof stubEventStream>;

beforeEach(() => {
  stream = stubEventStream();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ConnectionsSection", () => {
  it("renders empty state when there are no connections", async () => {
    stubRoutes({
      [`GET ${BASE}/connections`]: () => json([]),
    });

    renderSection(<ConnectionsSection projectId="prj_demo" />);

    expect(await screen.findByText("No connections yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add connection" })).toBeInTheDocument();
  });

  it("lists ok and unreachable connections showing status_detail for unreachable", async () => {
    stubRoutes({
      [`GET ${BASE}/connections`]: () => json([okConnection, unreachableConnection]),
      [`GET ${BASE}/connections/con_ok/tables`]: () => json([]),
    });

    renderSection(<ConnectionsSection projectId="prj_demo" />);

    expect(await screen.findByText("sales-data")).toBeInTheDocument();
    expect(screen.getByText("missing-data")).toBeInTheDocument();

    expect(screen.getByText("Folder does not exist")).toBeInTheDocument();

    const okBadges = screen.getAllByText("ok");
    expect(okBadges.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("unreachable")).toBeInTheDocument();
  });

  it("shows table count and toggles expanded table list with formats", async () => {
    const user = userEvent.setup();
    stubRoutes({
      [`GET ${BASE}/connections`]: () => json([okConnection]),
      [`GET ${BASE}/connections/con_ok/tables`]: () => json(sampleTables),
    });

    renderSection(<ConnectionsSection projectId="prj_demo" />);

    expect(await screen.findByText("2 tables")).toBeInTheDocument();

    const showBtn = screen.getByRole("button", { name: "Show tables" });
    expect(showBtn).toHaveAttribute("aria-expanded", "false");

    await user.click(showBtn);

    expect(showBtn).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("orders")).toBeInTheDocument();
    expect(screen.getByText("csv")).toBeInTheDocument();
    expect(screen.getByText("customers")).toBeInTheDocument();
    expect(screen.getByText("parquet")).toBeInTheDocument();

    const hideBtn = screen.getByRole("button", { name: "Hide tables" });
    await user.click(hideBtn);

    expect(screen.queryByText("orders")).not.toBeInTheDocument();
    expect(screen.queryByText("customers")).not.toBeInTheDocument();
  });

  it("renders ConnectionFindings for secret and unsupported files, and nothing for clean ones", async () => {
    const connectionWithFindings: ConnectionView = {
      ...okConnection,
      id: "con_findings",
      name: "findings-data",
      findings: {
        secret_files: [{ path: ".env", reason: "matches a secret file name" }],
        unsupported_files: [{ path: "notes.docx", reason: "not a supported format" }],
        truncated: false,
      },
    };

    stubRoutes({
      [`GET ${BASE}/connections`]: () => json([connectionWithFindings, okConnection]),
      [`GET ${BASE}/connections/con_findings/tables`]: () => json([]),
      [`GET ${BASE}/connections/con_ok/tables`]: () => json([]),
    });

    renderSection(<ConnectionsSection projectId="prj_demo" />);

    expect(await screen.findByText("findings-data")).toBeInTheDocument();
    expect(screen.getByText(".env")).toBeInTheDocument();
    expect(screen.getByText("matches a secret file name")).toBeInTheDocument();
    expect(screen.getByText("notes.docx")).toBeInTheDocument();
    expect(screen.getByText("not a supported format")).toBeInTheDocument();

    const alerts = screen.getAllByRole("alert");
    expect(alerts.length).toBe(1);
  });

  it("asks for confirmation on Remove, then calls DELETE and removes row", async () => {
    const user = userEvent.setup();
    let connectionsList = [okConnection];
    const requests = stubRoutes({
      [`GET ${BASE}/connections`]: () => json(connectionsList),
      [`GET ${BASE}/connections/con_ok/tables`]: () => json([]),
      [`DELETE ${BASE}/connections/con_ok`]: () => {
        connectionsList = [];
        return new Response(null, { status: 204 });
      },
    });

    renderSection(<ConnectionsSection projectId="prj_demo" />);

    expect(await screen.findByText("sales-data")).toBeInTheDocument();

    const removeBtn = screen.getByRole("button", { name: "Remove" });
    await user.click(removeBtn);

    expect(screen.getByRole("heading", { name: "Remove sales-data?" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "The connection will be removed from this project. The folder on disk is not touched.",
      ),
    ).toBeInTheDocument();

    const confirmBtn = screen.getByRole("button", { name: "Remove" });
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(screen.queryByText("sales-data")).not.toBeInTheDocument();
    });
    expect(screen.getByText("No connections yet.")).toBeInTheDocument();

    const deleteReq = requests.find((r) => r.method === "DELETE");
    expect(deleteReq).toBeDefined();
    expect(deleteReq?.path).toBe(`${BASE}/connections/con_ok`);
  });

  it("refreshes list when connection.created event is received", async () => {
    let connectionsList = [okConnection];

    stubRoutes({
      [`GET ${BASE}/connections`]: () => json(connectionsList),
      [`GET ${BASE}/connections/con_ok/tables`]: () => json([]),
      [`GET ${BASE}/connections/con_unreach/tables`]: () => json([]),
    });

    renderSection(<ConnectionsSection projectId="prj_demo" />);

    expect(await screen.findByText("sales-data")).toBeInTheDocument();
    expect(screen.queryByText("missing-data")).not.toBeInTheDocument();

    connectionsList = [okConnection, unreachableConnection];

    await act(async () => {
      stream.emit("connection.created", {
        id: "evt_1",
        seq: 1,
        ts: "2026-10-28T10:00:00Z",
        source: "connections",
        subject_id: "con_unreach",
        payload: { name: "missing-data", kind: "local_folder" },
        type: "connection.created",
      });
      await Promise.resolve();
    });

    expect(await screen.findByText("missing-data")).toBeInTheDocument();
  });

  it("renders data version and truncated indicator correctly", async () => {
    const truncatedConnection: ConnectionView = {
      ...okConnection,
      id: "con_trunc",
      name: "large-data",
      data_version: "v2_9876543210ab",
      findings: {
        secret_files: [{ path: "secret.key", reason: "matches a secret file name" }],
        unsupported_files: [],
        truncated: true,
      },
    };

    stubRoutes({
      [`GET ${BASE}/connections`]: () => json([truncatedConnection, unreachableConnection]),
      [`GET ${BASE}/connections/con_trunc/tables`]: () => json([]),
    });

    renderSection(<ConnectionsSection projectId="prj_demo" />);

    expect(await screen.findByText("large-data")).toBeInTheDocument();
    expect(screen.getByText("v2_9876543210ab")).toBeInTheDocument();
    expect(screen.getByText("and more")).toBeInTheDocument();

    expect(screen.getByText("missing-data")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });

  it("shows error alert when loading connections fails", async () => {
    stubRoutes({
      [`GET ${BASE}/connections`]: () => failure("Failed to load connections", 500),
    });

    renderSection(<ConnectionsSection projectId="prj_demo" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load connections");
  });
});
