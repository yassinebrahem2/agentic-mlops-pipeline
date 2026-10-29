import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { ConnectionDialog } from "./ConnectionDialog";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type ConnectionView = components["schemas"]["ConnectionView"];
type Findings = components["schemas"]["Findings"];

const BASE = "/api/projects/prj_demo";

const cleanFindings: Findings = {
  secret_files: [],
  unsupported_files: [],
  truncated: false,
};

const findingsWithSecrets: Findings = {
  secret_files: [{ path: ".env", reason: "matches a secret file name" }],
  unsupported_files: [{ path: "bad.bin", reason: "not a supported format" }],
  truncated: false,
};

const existingConnection: ConnectionView = {
  id: "con_123",
  name: "sales-data",
  kind: "local_folder",
  settings: { path: "/old/path" },
  status: "ok",
  status_detail: null,
  data_version: "v1_abc",
  findings: cleanFindings,
};

beforeEach(() => {
  stubEventStream();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ConnectionDialog", () => {
  it("choosing a folder calls inspect and shows its findings before Create is enabled", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const requests = stubRoutes({
      "GET /api/app": () =>
        json({ name: "mlops_agents", version: "0.1.0", mode: "local", data_dir: "/data" }),
      "GET /api/folders": () =>
        json({ path: "/data/folder", parent: "/data", roots: ["/"], entries: [] }),
      [`POST ${BASE}/connections/inspect`]: () => json(findingsWithSecrets),
    });

    renderSection(
      <ConnectionDialog projectId="prj_demo" open={true} onOpenChange={onOpenChange} />,
    );

    const createBtn = screen.getByRole("button", { name: "Create" });
    expect(createBtn).toBeDisabled();

    // Type name first
    const nameInput = screen.getByRole("textbox", { name: "Name" });
    await user.type(nameInput, "my-sales");
    expect(createBtn).toBeDisabled();

    // Choose folder from browser
    const chooseFolderBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseFolderBtn);

    // Findings appear
    expect(await screen.findByText(".env")).toBeInTheDocument();
    expect(screen.getByText("matches a secret file name")).toBeInTheDocument();
    expect(screen.getByText("bad.bin")).toBeInTheDocument();
    expect(screen.getByText("not a supported format")).toBeInTheDocument();

    // Now Create is enabled
    expect(createBtn).toBeEnabled();

    // Verify inspect request
    const inspectReq = requests.find((r) => r.path === `${BASE}/connections/inspect`);
    expect(inspectReq).toBeDefined();
    expect(inspectReq?.body).toEqual({
      kind: "local_folder",
      settings: { path: "/data/folder" },
    });
  });

  it("shows inspect refusal in an alert and keeps Create disabled", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    stubRoutes({
      "GET /api/app": () =>
        json({ name: "mlops_agents", version: "0.1.0", mode: "local", data_dir: "/data" }),
      "GET /api/folders": () =>
        json({ path: "/data/inside-app-data", parent: "/data", roots: ["/"], entries: [] }),
      [`POST ${BASE}/connections/inspect`]: () =>
        json(
          {
            code: "connection_invalid",
            message: "Folder is inside the data folder.",
            detail: null,
          },
          400,
        ),
    });

    renderSection(
      <ConnectionDialog projectId="prj_demo" open={true} onOpenChange={onOpenChange} />,
    );

    const nameInput = screen.getByRole("textbox", { name: "Name" });
    await user.type(nameInput, "bad-folder");

    const chooseFolderBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseFolderBtn);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Folder is inside the data folder.");

    const createBtn = screen.getByRole("button", { name: "Create" });
    expect(createBtn).toBeDisabled();
  });

  it("adding sends chosen path and name, invalidates connections, and closes dialog", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const requests = stubRoutes({
      "GET /api/app": () =>
        json({ name: "mlops_agents", version: "0.1.0", mode: "local", data_dir: "/data" }),
      "GET /api/folders": () =>
        json({ path: "/data/sales", parent: "/data", roots: ["/"], entries: [] }),
      [`POST ${BASE}/connections/inspect`]: () => json(cleanFindings),
      [`POST ${BASE}/connections`]: () =>
        json({
          id: "con_new",
          name: "my-sales",
          kind: "local_folder",
          settings: { path: "/data/sales" },
          status: "ok",
          status_detail: null,
          data_version: null,
          findings: cleanFindings,
        }),
    });

    renderSection(
      <ConnectionDialog projectId="prj_demo" open={true} onOpenChange={onOpenChange} />,
    );

    const nameInput = screen.getByRole("textbox", { name: "Name" });
    await user.type(nameInput, "my-sales");

    const chooseFolderBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseFolderBtn);

    const createBtn = await screen.findByRole("button", { name: "Create" });
    await waitFor(() => {
      expect(createBtn).toBeEnabled();
    });

    await user.click(createBtn);

    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });

    const createReq = requests.find((r) => r.path === `${BASE}/connections` && r.method === "POST");
    expect(createReq).toBeDefined();
    expect(createReq?.body).toEqual({
      kind: "local_folder",
      name: "my-sales",
      settings: { path: "/data/sales" },
    });
  });

  it("editing sends PATCH with new path and keeps name read-only", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const requests = stubRoutes({
      "GET /api/app": () =>
        json({ name: "mlops_agents", version: "0.1.0", mode: "local", data_dir: "/data" }),
      "GET /api/folders": () => json({ path: "/new/path", parent: "/", roots: ["/"], entries: [] }),
      [`POST ${BASE}/connections/inspect`]: () => json(cleanFindings),
      [`PATCH ${BASE}/connections/con_123`]: () =>
        json({
          ...existingConnection,
          settings: { path: "/new/path" },
        }),
    });

    renderSection(
      <ConnectionDialog
        projectId="prj_demo"
        connection={existingConnection}
        open={true}
        onOpenChange={onOpenChange}
      />,
    );

    const nameInput = screen.getByRole("textbox", { name: "Name" });
    expect(nameInput).toHaveValue("sales-data");
    expect(nameInput).toBeDisabled();

    const saveBtn = screen.getByRole("button", { name: "Save" });
    expect(saveBtn).toBeDisabled();

    const chooseFolderBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseFolderBtn);

    await waitFor(() => {
      expect(saveBtn).toBeEnabled();
    });

    await user.click(saveBtn);

    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });

    const patchReq = requests.find(
      (r) => r.path === `${BASE}/connections/con_123` && r.method === "PATCH",
    );
    expect(patchReq).toBeDefined();
    expect(patchReq?.body).toEqual({
      settings: { path: "/new/path" },
    });
  });

  it("409 name clash displays error message in dialog", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    stubRoutes({
      "GET /api/app": () =>
        json({ name: "mlops_agents", version: "0.1.0", mode: "local", data_dir: "/data" }),
      "GET /api/folders": () =>
        json({ path: "/data/test", parent: "/data", roots: ["/"], entries: [] }),
      [`POST ${BASE}/connections/inspect`]: () => json(cleanFindings),
      [`POST ${BASE}/connections`]: () =>
        failure("A connection with this name already exists.", 409),
    });

    renderSection(
      <ConnectionDialog projectId="prj_demo" open={true} onOpenChange={onOpenChange} />,
    );

    const nameInput = screen.getByRole("textbox", { name: "Name" });
    await user.type(nameInput, "taken-name");

    const chooseFolderBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseFolderBtn);

    const createBtn = await screen.findByRole("button", { name: "Create" });
    await waitFor(() => {
      expect(createBtn).toBeEnabled();
    });

    await user.click(createBtn);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("A connection with this name already exists.");
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("server mode shows 'This is a folder on the server.' and still allows Add", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const requests = stubRoutes({
      "GET /api/app": () =>
        json({ name: "mlops_agents", version: "0.1.0", mode: "server", data_dir: "/data" }),
      "GET /api/folders": () =>
        json({ path: "/server/folder", parent: "/server", roots: ["/"], entries: [] }),
      [`POST ${BASE}/connections/inspect`]: () => json(cleanFindings),
      [`POST ${BASE}/connections`]: () =>
        json({
          id: "con_srv",
          name: "srv-data",
          kind: "local_folder",
          settings: { path: "/server/folder" },
          status: "ok",
          status_detail: null,
          data_version: null,
          findings: cleanFindings,
        }),
    });

    renderSection(
      <ConnectionDialog projectId="prj_demo" open={true} onOpenChange={onOpenChange} />,
    );

    expect(await screen.findByText("This is a folder on the server.")).toBeInTheDocument();

    const nameInput = screen.getByRole("textbox", { name: "Name" });
    await user.type(nameInput, "srv-data");

    const chooseFolderBtn = await screen.findByRole("button", { name: "Use this folder" });
    await user.click(chooseFolderBtn);

    const createBtn = await screen.findByRole("button", { name: "Create" });
    await waitFor(() => {
      expect(createBtn).toBeEnabled();
    });

    await user.click(createBtn);

    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });

    const createReq = requests.find((r) => r.path === `${BASE}/connections` && r.method === "POST");
    expect(createReq).toBeDefined();
    expect(createReq?.body).toEqual({
      kind: "local_folder",
      name: "srv-data",
      settings: { path: "/server/folder" },
    });
  });
});
