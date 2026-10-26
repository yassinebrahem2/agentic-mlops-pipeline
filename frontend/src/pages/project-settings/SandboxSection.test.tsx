import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { SandboxSection } from "./SandboxSection";
import { bodyField, failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type View = components["schemas"]["SandboxView"];
type Environment = components["schemas"]["EnvironmentStatus"];
type BuildEvent = "sandbox.build.started" | "sandbox.build.finished" | "sandbox.build.failed";

const BASE = "/api/projects/prj_demo";
const ready: Environment = {
  state: "ready",
  tag: "mlops-env:abc",
  detail: "Built from the lockfile",
};
const rebuilt: Environment = { state: "ready", tag: "mlops-env:def", detail: "Rebuilt" };
const building: Environment = { state: "building", tag: null, detail: "Installing" };
const docker = { available: true, version: "27.1", detail: null };

function setup(
  options: {
    view?: Partial<View>;
    gatedRebuild?: boolean;
    getFails?: boolean;
    putFails?: boolean;
    rebuildFails?: boolean;
  } = {},
) {
  const stream = stubEventStream();
  let view: View = { docker, environment: ready, network: false, ...options.view };
  let release: ((environment: Environment) => void) | undefined;
  const requests = stubRoutes({
    [`GET ${BASE}/sandbox`]: () =>
      options.getFails ? failure("Sandbox unavailable", 500) : json(view),
    [`PUT ${BASE}/settings/sandbox`]: (body) => {
      if (options.putFails) return failure("Network setting not saved");
      const next = bodyField(body, "network");
      if (typeof next === "boolean") view = { ...view, network: next };
      return json({ network: view.network });
    },
    // The real endpoint answers once the build is over, with the final state.
    [`POST ${BASE}/sandbox/rebuild`]: async () => {
      if (options.rebuildFails) return failure("Rebuild refused");
      const environment = options.gatedRebuild
        ? await new Promise<Environment>((resolve) => {
            release = resolve;
          })
        : rebuilt;
      view = { ...view, environment };
      return json(environment);
    },
  });
  renderSection(<SandboxSection projectId="prj_demo" />);
  return {
    requests,
    user: userEvent.setup(),
    /** The server's state changes; the page learns of it from an event. */
    setEnvironment: (environment: Environment) => {
      view = { ...view, environment };
    },
    emit: async (type: BuildEvent) => {
      await act(async () => {
        stream.emit(type, {
          id: "evt_1",
          seq: 1,
          ts: "2026-10-26T10:00:00Z",
          source: "sandbox",
          subject_id: null,
          payload: {},
          type,
        });
        await Promise.resolve();
      });
    },
    finishRebuild: async (environment: Environment) => {
      await act(async () => {
        release?.(environment);
        await Promise.resolve();
      });
    },
    sandboxGets: () =>
      requests.filter((r) => r.method === "GET" && r.path === `${BASE}/sandbox`).length,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("shows Docker status and the environment image as the server sent them", async () => {
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

it("groups each server message under its own term", async () => {
  setup({
    view: {
      docker: { available: true, version: "27.1", detail: "Docker is slow" },
      environment: { state: "failed", tag: "mlops-env:abc", detail: "Build log tail" },
    },
  });
  const dockerMessage = (await screen.findByText("Docker message")).nextElementSibling;
  expect(dockerMessage).toHaveTextContent("Docker is slow");
  expect(screen.getByText("Image message").nextElementSibling).toHaveTextContent("Build log tail");
});

it("disables Rebuild while the image is building", async () => {
  setup({ view: { environment: building } });
  expect(await screen.findByText("Installing")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Building..." })).toBeDisabled();
});

it.each(["none", "ready", "failed"] as const)(
  "rebuilds from the %s state and shows the final answer",
  async (state) => {
    const { user, requests } = setup({
      view: { environment: { state, tag: null, detail: "Previous detail" } },
    });
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));
    expect(await screen.findByText("Rebuilt")).toBeInTheDocument();
    expect(screen.getByText("mlops-env:def")).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "POST")).toEqual([
      { path: `${BASE}/sandbox/rebuild`, method: "POST", body: null },
    ]);
    expect(screen.getByRole("button", { name: "Rebuild" })).toBeEnabled();
  },
);

it("disables Rebuild while the rebuild request is running", async () => {
  const { user, finishRebuild } = setup({ gatedRebuild: true });
  await user.click(await screen.findByRole("button", { name: "Rebuild" }));
  expect(await screen.findByRole("button", { name: "Building..." })).toBeDisabled();
  await finishRebuild(rebuilt);
  expect(await screen.findByRole("button", { name: "Rebuild" })).toBeEnabled();
});

it("shows the server's error for a failed rebuild", async () => {
  const { user } = setup({ rebuildFails: true });
  await user.click(await screen.findByRole("button", { name: "Rebuild" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Rebuild refused");
});

it("does not ask for the status again without an event", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const { sandboxGets } = setup({ view: { environment: building } });
  expect(await screen.findByText("Building")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(sandboxGets()).toBe(1);
});

it("shows a build started elsewhere and then its result", async () => {
  const { emit, setEnvironment } = setup();
  expect(await screen.findByText("Ready")).toBeInTheDocument();
  setEnvironment(building);
  await emit("sandbox.build.started");
  expect(await screen.findByText("Building")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Building..." })).toBeDisabled();
  setEnvironment(rebuilt);
  await emit("sandbox.build.finished");
  expect(await screen.findByText("mlops-env:def")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Rebuild" })).toBeEnabled();
});

it("shows a failed build reported by an event", async () => {
  const { emit, setEnvironment } = setup();
  expect(await screen.findByText("Ready")).toBeInTheDocument();
  setEnvironment({ state: "failed", tag: null, detail: "Build log tail" });
  await emit("sandbox.build.failed");
  expect(await screen.findByText("Build log tail")).toBeInTheDocument();
  expect(screen.getByText("Failed")).toBeInTheDocument();
});

it("ends on the final state when the finished event comes before the rebuild answer", async () => {
  const { user, emit, setEnvironment, finishRebuild, sandboxGets } = setup({ gatedRebuild: true });
  await user.click(await screen.findByRole("button", { name: "Rebuild" }));
  setEnvironment(rebuilt);
  await emit("sandbox.build.finished");
  expect(await screen.findByText("mlops-env:def")).toBeInTheDocument();
  await finishRebuild(rebuilt);
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Rebuild" })).toBeEnabled();
  });
  expect(screen.getByText("mlops-env:def")).toBeInTheDocument();
  expect(sandboxGets()).toBeGreaterThan(1);
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
