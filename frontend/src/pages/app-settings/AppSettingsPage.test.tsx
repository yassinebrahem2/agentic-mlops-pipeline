import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import { renderPage } from "../../test/render";

type AppInfo = components["schemas"]["AppInfo"];
type Sent = { method: string; url: URL; body: unknown };
type Handler = (sent: Sent) => Response | Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(handlers: Record<string, Handler>): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const text = await input.text();
      const request: Sent = {
        method: input.method,
        url: new URL(input.url),
        body: text ? (JSON.parse(text) as unknown) : undefined,
      };
      sent.push(request);
      const handler = handlers[`${request.method} ${request.url.pathname}`];
      if (!handler) throw new Error(`Unexpected ${request.method} ${request.url.pathname}`);
      return handler(request);
    }),
  );
  return sent;
}

const localApp: AppInfo = {
  name: "example-app",
  version: "0.1.0",
  mode: "local",
  data_dir: "/home/someone/.example-app",
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("App settings page", () => {
  it("shows the app's name, version, run mode and data folder", async () => {
    stubApi({ "GET /api/app": () => json(localApp) });
    renderPage("/settings");

    expect(await screen.findByText("example-app")).toBeInTheDocument();
    expect(screen.getByText("0.1.0")).toBeInTheDocument();
    expect(screen.getByText("Local")).toBeInTheDocument();
    expect(screen.getByText("/home/someone/.example-app")).toBeInTheDocument();
  });

  it("shows the server run mode", async () => {
    stubApi({ "GET /api/app": () => json({ ...localApp, mode: "server" }) });
    renderPage("/settings");

    expect(await screen.findByText("Server")).toBeInTheDocument();
    expect(screen.queryByText("Local")).not.toBeInTheDocument();
  });

  it("copies the data folder", async () => {
    stubApi({ "GET /api/app": () => json(localApp) });
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    renderPage("/settings");

    await user.click(await screen.findByRole("button", { name: "Copy data folder" }));

    expect(writeText).toHaveBeenCalledWith("/home/someone/.example-app");
    expect(await screen.findByText("Copied.")).toBeInTheDocument();
  });

  it("says so when the data folder cannot be copied", async () => {
    stubApi({ "GET /api/app": () => json(localApp) });
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    renderPage("/settings");

    await user.click(await screen.findByRole("button", { name: "Copy data folder" }));

    expect(
      await screen.findByText("Could not copy. Select the folder path and copy it by hand."),
    ).toBeInTheDocument();
  });

  it("backs up now and shows the folder the backup was written to", async () => {
    const backup = "/home/someone/.example-app/backups/snapshots/20261021T120000Z";
    const sent = stubApi({
      "GET /api/app": () => json(localApp),
      "POST /api/app/backup": () => json({ path: backup }),
    });
    const user = userEvent.setup();
    renderPage("/settings");

    await user.click(await screen.findByRole("button", { name: "Back up now" }));

    expect(await screen.findByText(backup)).toBeInTheDocument();
    expect(
      sent.filter((s) => s.method !== "GET").map((s) => [s.method, s.url.pathname, s.body]),
    ).toEqual([["POST", "/api/app/backup", undefined]]);
  });

  it("shows the server's message when the backup fails", async () => {
    stubApi({
      "GET /api/app": () => json(localApp),
      "POST /api/app/backup": () =>
        json(
          {
            code: "internal_error",
            message: "Something went wrong on the server. The server log has the details.",
            detail: null,
          },
          500,
        ),
    });
    const user = userEvent.setup();
    renderPage("/settings");

    await user.click(await screen.findByRole("button", { name: "Back up now" }));

    expect(
      await screen.findByText(
        "Something went wrong on the server. The server log has the details.",
      ),
    ).toBeInTheDocument();
  });

  it("says so when the app information cannot be read", async () => {
    stubApi({
      "GET /api/app": () =>
        json(
          { code: "keystore_unavailable", message: "The key store is locked.", detail: null },
          503,
        ),
    });
    renderPage("/settings");

    expect(await screen.findByText("The key store is locked.")).toBeInTheDocument();
  });
});
