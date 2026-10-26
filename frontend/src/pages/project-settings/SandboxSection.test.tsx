import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { SandboxSection } from "./SandboxSection";
import { bodyField, failure, json, renderSection, stubRoutes } from "./testing";

type View = components["schemas"]["SandboxView"];
type Environment = components["schemas"]["EnvironmentStatus"];

const BASE = "/api/projects/prj_demo";
const ready: Environment = {
  state: "ready",
  tag: "mlops-env:abc",
  detail: "Built from the lockfile",
};
const docker = { available: true, version: "27.1", detail: null };

function setup(
  options: {
    view?: Partial<View>;
    rebuilt?: Environment;
    getFails?: boolean;
    putFails?: boolean;
    rebuildFails?: boolean;
  } = {},
) {
  let view: View = { docker, environment: ready, network: false, ...options.view };
  const requests = stubRoutes({
    [`GET ${BASE}/sandbox`]: () =>
      options.getFails ? failure("Sandbox unavailable", 500) : json(view),
    [`PUT ${BASE}/settings/sandbox`]: (body) => {
      if (options.putFails) return failure("Network setting not saved");
      const next = bodyField(body, "network");
      if (typeof next === "boolean") view = { ...view, network: next };
      return json({ network: view.network });
    },
    [`POST ${BASE}/sandbox/rebuild`]: () => {
      if (options.rebuildFails) return failure("Rebuild refused");
      view = {
        ...view,
        environment: options.rebuilt ?? { state: "building", tag: null, detail: null },
      };
      return json(view.environment);
    },
  });
  renderSection(<SandboxSection projectId="prj_demo" />);
  return { requests, user: userEvent.setup() };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("shows Docker's status and the environment image as the server sent them", async () => {
  setup();
  expect(await screen.findByText("Available")).toBeInTheDocument();
  expect(screen.getByText("27.1")).toBeInTheDocument();
  expect(screen.getByText("Ready")).toBeInTheDocument();
  expect(screen.getByText("mlops-env:abc")).toBeInTheDocument();
  expect(screen.getByText("Built from the lockfile")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "Network access" })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Rebuild" })).toBeEnabled();
});

it("says what the server said when Docker is not available and disables Rebuild", async () => {
  setup({
    view: {
      docker: { available: false, version: null, detail: "Docker is not running" },
      environment: { state: "unavailable", tag: null, detail: "No Docker, no image" },
    },
  });
  expect(await screen.findByText("Docker is not running")).toBeInTheDocument();
  expect(screen.getByText("Not available")).toBeInTheDocument();
  expect(screen.getByText("No Docker, no image")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Rebuild" })).toBeDisabled();
});

it("disables Rebuild while the image is building", async () => {
  setup({ view: { environment: { state: "building", tag: null, detail: "Installing" } } });
  expect(await screen.findByText("Installing")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Rebuild" })).toBeDisabled();
});

it.each(["none", "ready", "failed"] as const)(
  "rebuilds from the %s state and shows the answer",
  async (state) => {
    const { user, requests } = setup({
      view: { environment: { state, tag: null, detail: "Previous detail" } },
      rebuilt: { state: "building", tag: null, detail: "Started" },
    });
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));
    expect(await screen.findByText("Started")).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "POST")).toEqual([
      { path: `${BASE}/sandbox/rebuild`, method: "POST", body: null },
    ]);
    expect(screen.getByRole("button", { name: "Rebuild" })).toBeDisabled();
  },
);

it("shows the server's error for a failed rebuild", async () => {
  const { user } = setup({ rebuildFails: true });
  await user.click(await screen.findByRole("button", { name: "Rebuild" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Rebuild refused");
});

it("checks the status again while the image is building", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const { requests } = setup({
    view: { environment: { state: "building", tag: null, detail: null } },
  });
  expect(await screen.findByText("Building")).toBeInTheDocument();
  const gets = () => requests.filter((r) => r.method === "GET").length;
  const before = gets();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(gets()).toBeGreaterThan(before);
});

it("saves the network switch", async () => {
  const { user, requests } = setup();
  const toggle = await screen.findByRole("checkbox", { name: "Network access" });
  await user.click(toggle);
  await waitFor(() => {
    expect(toggle).toBeChecked();
  });
  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
    { network: true },
  ]);
});

it("keeps the stored network value and shows the server's error when saving fails", async () => {
  const { user } = setup({ putFails: true });
  const toggle = await screen.findByRole("checkbox", { name: "Network access" });
  await user.click(toggle);
  expect(await screen.findByRole("alert")).toHaveTextContent("Network setting not saved");
  expect(toggle).not.toBeChecked();
});

it("shows the server's error for a failed load", async () => {
  setup({ getFails: true });
  expect(await screen.findByRole("alert")).toHaveTextContent("Sandbox unavailable");
});
