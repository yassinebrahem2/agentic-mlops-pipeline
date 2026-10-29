import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { formatTime } from "@/lib/format";
import type { EventRecord } from "@/lib/useProjectEvents";
import { TrackingSourcesSection } from "./TrackingSourcesSection";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type TrackingSourceView = components["schemas"]["TrackingSourceView"];

const BASE = "/api/projects/prj_demo";

const mlflowSource: TrackingSourceView = {
  id: "src_mlflow",
  type: "mlflow",
  settings: { url: "http://mlflow.local:5000" },
  auth: "credential",
  credential_set: true,
  last_sync: "2026-10-29T10:00:00Z",
  last_error: null,
};

const ghActionsSource: TrackingSourceView = {
  id: "src_gh",
  type: "github_actions",
  settings: { repository: "org/repo", workflows: ["ci.yml", "release.yml"] },
  auth: "none",
  credential_set: false,
  last_sync: null,
  last_error: "Workflow run failed with status 500",
};

const missingCredSource: TrackingSourceView = {
  id: "src_missing",
  type: "mlflow",
  settings: { url: "http://mlflow.unauthed:5000" },
  auth: "credential",
  credential_set: false,
  last_sync: null,
  last_error: null,
};

let stream: ReturnType<typeof stubEventStream>;

beforeEach(() => {
  stream = stubEventStream();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("TrackingSourcesSection", () => {
  it("renders empty state when there are no sources", async () => {
    stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => json([]),
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    expect(
      await screen.findByText(
        "No tracking source yet. Onboarding adds them when you approve the handoff, or add one here.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add tracking source" })).toBeInTheDocument();
  });

  it("shows route error when fetching sources fails", async () => {
    stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => failure("Failed to load tracking sources", 500),
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    expect(await screen.findByText("Failed to load tracking sources")).toBeInTheDocument();
  });

  it("renders sources with target, auth state, last sync and error across the three auth states", async () => {
    stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () =>
        json([mlflowSource, ghActionsSource, missingCredSource]),
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    // MLflow sources
    expect(await screen.findAllByText("MLflow")).toHaveLength(2);
    expect(screen.getByText("http://mlflow.local:5000")).toBeInTheDocument();
    expect(screen.getByText("Credential set")).toBeInTheDocument();
    expect(screen.getByText(`Last sync ${formatTime(mlflowSource.last_sync)}`)).toBeInTheDocument();

    // GitHub Actions source
    expect(screen.getByText("GitHub Actions")).toBeInTheDocument();
    expect(screen.getByText("org/repo (ci.yml, release.yml)")).toBeInTheDocument();
    expect(screen.getByText("No authentication")).toBeInTheDocument();
    expect(screen.getAllByText("Never synced")).toHaveLength(2);
    expect(screen.getByText("Workflow run failed with status 500")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Workflow run failed with status 500");

    // Missing credential source
    expect(screen.getByText("http://mlflow.unauthed:5000")).toBeInTheDocument();
    expect(screen.getByText("Credential missing")).toBeInTheDocument();
  });

  it("adds an mlflow source with a credential: posts source then puts credential", async () => {
    const user = userEvent.setup();
    let sourcesList: TrackingSourceView[] = [];

    const requests = stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => json(sourcesList),
      [`POST ${BASE}/tracking/sources`]: (body) => {
        const newSrc: TrackingSourceView = {
          id: "src_new_mlflow",
          type: "mlflow",
          settings: (body as { settings: Record<string, unknown> }).settings,
          auth: (body as { auth: "credential" | "none" }).auth,
          credential_set: false,
          last_sync: null,
          last_error: null,
        };
        sourcesList = [newSrc];
        return json(newSrc);
      },
      [`PUT ${BASE}/tracking/sources/src_new_mlflow/credential`]: () => {
        const existing = sourcesList[0];
        if (existing) {
          sourcesList = [{ ...existing, credential_set: true }];
        }
        return json(sourcesList[0]);
      },
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    expect(await screen.findByRole("button", { name: "Add tracking source" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add tracking source" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();

    const urlInput = within(dialog).getByLabelText("URL");
    await user.type(urlInput, "http://mlflow.example.com:5000");

    const credSwitch = within(dialog).getByRole("switch", { name: "Needs a credential" });
    await user.click(credSwitch);

    const credInput = within(dialog).getByLabelText("Credential");
    expect(credInput).toHaveAttribute("type", "password");
    await user.type(credInput, "my-secret-token");

    await user.click(within(dialog).getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    const postReq = requests.find(
      (r) => r.method === "POST" && r.path === `${BASE}/tracking/sources`,
    );
    expect(postReq?.body).toEqual({
      type: "mlflow",
      settings: { url: "http://mlflow.example.com:5000" },
      auth: "credential",
    });

    const putReq = requests.find(
      (r) => r.method === "PUT" && r.path === `${BASE}/tracking/sources/src_new_mlflow/credential`,
    );
    expect(putReq?.body).toEqual({
      credential: "my-secret-token",
    });
  });

  it("adds a github_actions source with auth none and shows no credential form", async () => {
    const user = userEvent.setup();
    let sourcesList: TrackingSourceView[] = [];

    const requests = stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => json(sourcesList),
      [`POST ${BASE}/tracking/sources`]: (body) => {
        const newSrc: TrackingSourceView = {
          id: "src_new_gh",
          type: "github_actions",
          settings: (body as { settings: Record<string, unknown> }).settings,
          auth: "none",
          credential_set: false,
          last_sync: null,
          last_error: null,
        };
        sourcesList = [newSrc];
        return json(newSrc);
      },
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    await user.click(await screen.findByRole("button", { name: "Add tracking source" }));
    const dialog = screen.getByRole("dialog");

    // Select GitHub Actions type
    const selectTrigger = within(dialog).getByRole("combobox", { name: "Type" });
    await user.click(selectTrigger);
    await user.click(await screen.findByRole("option", { name: "GitHub Actions" }));

    expect(within(dialog).queryByLabelText("Credential")).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole("switch", { name: "Needs a credential" }),
    ).not.toBeInTheDocument();
    expect(within(dialog).getByText(/uses the app's github login/i)).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("Repository"), "facebook/react");
    await user.type(within(dialog).getByLabelText("Workflows"), "ci.yml, release.yml");

    await user.click(within(dialog).getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    const postReq = requests.find(
      (r) => r.method === "POST" && r.path === `${BASE}/tracking/sources`,
    );
    expect(postReq?.body).toEqual({
      type: "github_actions",
      settings: {
        repository: "facebook/react",
        workflows: ["ci.yml", "release.yml"],
      },
      auth: "none",
    });
    expect(requests.some((r) => r.method === "PUT")).toBe(false);
  });

  it("shows 422 error message and keeps dialog open", async () => {
    const user = userEvent.setup();
    stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => json([]),
      [`POST ${BASE}/tracking/sources`]: () =>
        json(
          {
            code: "invalid_request",
            message: "Validation failed: invalid repository",
            detail: {
              errors: [{ field: "body.settings.repository", message: "Invalid repo format" }],
            },
          },
          422,
        ),
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    await user.click(await screen.findByRole("button", { name: "Add tracking source" }));
    const dialog = screen.getByRole("dialog");

    const selectTrigger = within(dialog).getByRole("combobox", { name: "Type" });
    await user.click(selectTrigger);
    await user.click(await screen.findByRole("option", { name: "GitHub Actions" }));

    await user.type(within(dialog).getByLabelText("Repository"), "invalid-repo");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));

    expect(
      await within(dialog).findByText("Validation failed: invalid repository"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Invalid repo format")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("leaves source as Credential missing with error shown when credential put fails", async () => {
    const user = userEvent.setup();
    let sourcesList: TrackingSourceView[] = [];

    stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => json(sourcesList),
      [`POST ${BASE}/tracking/sources`]: (body) => {
        const newSrc: TrackingSourceView = {
          id: "src_failed_cred",
          type: "mlflow",
          settings: (body as { settings: Record<string, unknown> }).settings,
          auth: "credential",
          credential_set: false,
          last_sync: null,
          last_error: null,
        };
        sourcesList = [newSrc];
        return json(newSrc);
      },
      [`PUT ${BASE}/tracking/sources/src_failed_cred/credential`]: () =>
        failure("Rejected token: permission denied", 401),
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    await user.click(await screen.findByRole("button", { name: "Add tracking source" }));
    const dialog = screen.getByRole("dialog");

    await user.type(within(dialog).getByLabelText("URL"), "http://mlflow.example.com");
    await user.click(within(dialog).getByRole("switch", { name: "Needs a credential" }));
    await user.type(within(dialog).getByLabelText("Credential"), "bad-token");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));

    // Dialog closes
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    // Source listed as Credential missing
    expect(await screen.findByText("Credential missing")).toBeInTheDocument();
    // Credential form open on its row with error shown
    expect(screen.getByText("Rejected token: permission denied")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set" })).toBeInTheDocument();
  });

  it("clears credential from input, leaves it absent from DOM and URLs", async () => {
    const user = userEvent.setup();
    const requests = stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => json([missingCredSource]),
      [`PUT ${BASE}/tracking/sources/src_missing/credential`]: () =>
        json({ ...missingCredSource, credential_set: true }),
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    expect(await screen.findByText("Credential missing")).toBeInTheDocument();

    const credInput = screen.getByLabelText("Credential");
    expect(credInput).toHaveAttribute("type", "password");
    expect(credInput).toHaveAttribute("autocomplete", "off");

    const secretValue = "super-secret-password-xyz-987";
    await user.type(credInput, secretValue);
    expect(credInput).toHaveValue(secretValue);

    await user.click(screen.getByRole("button", { name: "Set" }));

    await waitFor(() => {
      expect(credInput).toHaveValue("");
    });

    // Absent from DOM
    expect(document.body.innerHTML).not.toContain(secretValue);

    // Absent from URLs
    for (const req of requests) {
      expect(req.path).not.toContain(secretValue);
    }
  });

  it("refreshes last sync on tracking.synced event", async () => {
    let currentSources: TrackingSourceView[] = [
      {
        ...mlflowSource,
        last_sync: "2026-10-29T10:00:00Z",
      },
    ];

    stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => json(currentSources),
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    expect(
      await screen.findByText(`Last sync ${formatTime("2026-10-29T10:00:00Z")}`),
    ).toBeInTheDocument();

    // Change server state
    currentSources = [
      {
        ...mlflowSource,
        last_sync: "2026-10-29T11:30:00Z",
      },
    ];

    // Emit tracking.synced event
    act(() => {
      stream.emitOpen();
      const mockEvent: EventRecord = {
        id: "ev_sync_1",
        seq: 1,
        ts: "2026-10-29T11:30:00Z",
        source: "orchestrator",
        type: "tracking.synced",
        subject_id: "prj_demo",
        payload: {},
      };
      stream.emit("tracking.synced", mockEvent, 1);
    });

    await waitFor(
      () => {
        expect(
          screen.getByText(`Last sync ${formatTime("2026-10-29T11:30:00Z")}`),
        ).toBeInTheDocument();
      },
      { timeout: 3000 },
    );
  });

  it("safely handles tracking source with undefined or null settings", async () => {
    const rawSource = {
      id: "src_corrupt",
      type: "github_actions",
      settings: undefined as unknown as Record<string, unknown>,
      auth: "none",
      credential_set: false,
      last_sync: null,
      last_error: null,
    } as unknown as TrackingSourceView;

    stubRoutes({
      [`GET ${BASE}/tracking/sources`]: () => json([rawSource]),
    });

    renderSection(<TrackingSourcesSection projectId="prj_demo" />);

    expect(await screen.findByText("GitHub Actions")).toBeInTheDocument();
    expect(screen.getByText("all workflows")).toBeInTheDocument();
  });
});
