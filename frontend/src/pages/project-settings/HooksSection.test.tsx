import { QueryClient } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { HooksSection } from "./HooksSection";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type Hook = components["schemas"]["HookView"];

const BASE = "/api/projects/prj_demo/hooks";
const SECRET = "tok-super-secret-9f2";

const ACTIVE: Hook = {
  id: "hook_train",
  name: "Train churn",
  model: "churn",
  spec: {
    kind: "train",
    type: "ci_dispatch",
    template: null,
    workflow: "train.yml",
    inputs: {},
    placeholders: [],
    deploys: false,
    entry_files: [],
  },
  status: "active",
  suspended_reason: null,
  credential_set: false,
  pins: [{ path: ".github/workflows/train.yml", sha256: "abcdef0123456789abcdef" }],
  approved_commit: "c0ffee",
  deploys_verified: null,
  source: "handoff",
  created: "2026-10-30T09:00:00Z",
};
const SUSPENDED: Hook = {
  ...ACTIVE,
  id: "hook_deploy",
  name: "Deploy churn",
  spec: { ...ACTIVE.spec, kind: "deploy", deploys: true },
  status: "suspended",
  suspended_reason: "The workflow file changed since it was approved.",
  credential_set: true,
  deploys_verified: true,
};

let stream: ReturnType<typeof stubEventStream>;

beforeEach(() => {
  stream = stubEventStream();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function setup(options: { hooks?: Hook[]; routes?: Parameters<typeof stubRoutes>[0] } = {}) {
  let hooks = options.hooks ?? [ACTIVE, SUSPENDED];
  const requests = stubRoutes({
    [`GET ${BASE}`]: () => json(hooks),
    [`GET /api/projects/prj_demo/training/policies`]: () => json([]),
    ...options.routes,
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  renderSection(<HooksSection projectId="prj_demo" />, client);
  return {
    requests,
    client,
    user: userEvent.setup(),
    setHooks: (next: Hook[]) => {
      hooks = next;
    },
  };
}

function row(name: string): HTMLElement {
  return screen.getByRole("listitem", { name });
}

describe("HooksSection", () => {
  it("renders each hook with its kind, model, status, reason, deploys, pins", async () => {
    setup();

    const train = within(await screen.findByRole("listitem", { name: "Train churn" }));
    expect(train.getByText("Train")).toBeInTheDocument();
    expect(train.getByText("churn")).toBeInTheDocument();
    expect(train.getByText("Active")).toBeInTheDocument();
    expect(train.getByTestId("hook-deploys")).toHaveTextContent("No");
    expect(train.getByTestId("hook-deploys-verified")).toHaveTextContent("Not verified");
    expect(train.getByText(".github/workflows/train.yml")).toBeInTheDocument();
    expect(train.getByText(/abcdef012345/)).toBeInTheDocument();
    expect(train.getByTestId("hook-credential")).toHaveTextContent("No credential");

    const deploy = within(row("Deploy churn"));
    expect(deploy.getByText("Suspended")).toBeInTheDocument();
    expect(deploy.getByText("The workflow file changed since it was approved.")).toBeVisible();
    expect(deploy.getByTestId("hook-deploys")).toHaveTextContent("Yes");
    expect(deploy.getByTestId("hook-deploys-verified")).toHaveTextContent("Yes");
    expect(deploy.getByTestId("hook-credential")).toHaveTextContent("Credential set");
  });

  it("says so when there are no hooks", async () => {
    setup({ hooks: [] });

    expect(await screen.findByText("No hooks yet.")).toBeInTheDocument();
  });

  it("shows the server's error with a retry when the list cannot load", async () => {
    const { user } = setup({
      routes: { [`GET ${BASE}`]: () => failure("Hooks unavailable", 500) },
    });

    expect(await screen.findByText("Hooks unavailable")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Hooks unavailable")).toBeInTheDocument();
  });

  it("disables an active hook and the row shows the new status", async () => {
    const put = vi.fn<(body: unknown) => Response>(() => json({ ...ACTIVE, status: "disabled" }));
    const { user, requests } = setup({ routes: { [`PUT ${BASE}/hook_train/enabled`]: put } });

    const train = await screen.findByRole("listitem", { name: "Train churn" });
    await user.click(within(train).getByRole("button", { name: "Disable" }));

    await waitFor(() => {
      expect(within(row("Train churn")).getByText("Disabled")).toBeInTheDocument();
    });
    expect(put).toHaveBeenCalledWith({ enabled: false });
    expect(within(row("Train churn")).getByRole("button", { name: "Enable" })).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "PUT")).toHaveLength(1);
  });

  it("shows a refused enable as the server wrote it", async () => {
    const { user } = setup({
      routes: {
        [`PUT ${BASE}/hook_train/enabled`]: () => failure("Hook is suspended.", 409),
      },
    });

    const train = await screen.findByRole("listitem", { name: "Train churn" });
    await user.click(within(train).getByRole("button", { name: "Disable" }));

    expect(await within(row("Train churn")).findByText("Hook is suspended.")).toBeInTheDocument();
  });

  it("offers Approve changed content only for a suspended hook, and approving updates the row", async () => {
    const post = vi.fn(() => json({ ...SUSPENDED, status: "active", suspended_reason: null }));
    const { user } = setup({ routes: { [`POST ${BASE}/hook_deploy/approve`]: post } });

    const train = await screen.findByRole("listitem", { name: "Train churn" });
    expect(within(train).queryByRole("button", { name: "Approve changed content" })).toBeNull();
    await user.click(
      within(row("Deploy churn")).getByRole("button", { name: "Approve changed content" }),
    );

    await waitFor(() => {
      expect(within(row("Deploy churn")).getByText("Active")).toBeInTheDocument();
    });
    expect(post).toHaveBeenCalledTimes(1);
    expect(
      within(row("Deploy churn")).queryByRole("button", { name: "Approve changed content" }),
    ).toBeNull();
  });

  it("shows a refused approval as the server wrote it", async () => {
    const { user } = setup({
      routes: { [`POST ${BASE}/hook_deploy/approve`]: () => failure("Nothing to approve.", 409) },
    });

    await screen.findByRole("listitem", { name: "Deploy churn" });
    await user.click(
      within(row("Deploy churn")).getByRole("button", { name: "Approve changed content" }),
    );

    expect(await screen.findByText("Nothing to approve.")).toBeInTheDocument();
  });

  it("sets the credential write-only: password field, cleared after saving, in no response or cache", async () => {
    const put = vi.fn<(body: unknown) => Response>(() => json({ ...ACTIVE, credential_set: true }));
    const { user, client } = setup({ routes: { [`PUT ${BASE}/hook_train/credential`]: put } });

    const train = await screen.findByRole("listitem", { name: "Train churn" });
    const field = within(train).getByLabelText("Credential");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "new-password");
    expect(within(train).getByRole("button", { name: "Set credential" })).toBeDisabled();
    await user.type(field, SECRET);
    await user.click(within(train).getByRole("button", { name: "Set credential" }));

    await waitFor(() => {
      expect(within(row("Train churn")).getByTestId("hook-credential")).toHaveTextContent(
        "Credential set",
      );
    });
    expect(put).toHaveBeenCalledWith({ credential: SECRET });
    expect(within(row("Train churn")).getByLabelText("Credential")).toHaveValue("");
    const cached = JSON.stringify({
      queries: client
        .getQueryCache()
        .getAll()
        .map((q) => q.state),
      mutations: client
        .getMutationCache()
        .getAll()
        .map((m) => m.state),
    });
    expect(cached).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it("shows a refused credential without the secret, clears the field and does not retry", async () => {
    const put = vi.fn<(body: unknown) => Response>(() =>
      failure("Hook credential store is locked.", 500),
    );
    const { user, client } = setup({ routes: { [`PUT ${BASE}/hook_train/credential`]: put } });

    const train = await screen.findByRole("listitem", { name: "Train churn" });
    await user.type(within(train).getByLabelText("Credential"), SECRET);
    await user.click(within(train).getByRole("button", { name: "Set credential" }));

    expect(await screen.findByText("Hook credential store is locked.")).toBeInTheDocument();
    expect(within(row("Train churn")).getByLabelText("Credential")).toHaveValue("");
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(
      JSON.stringify(
        client
          .getMutationCache()
          .getAll()
          .map((m) => m.state),
      ),
    ).not.toContain(SECRET);
    await act(() => Promise.resolve());
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("shows a 422 with its field details and never the secret", async () => {
    const put = vi.fn<(body: unknown) => Response>(() =>
      json(
        {
          code: "invalid_request",
          message: "Some fields are missing or invalid.",
          detail: {
            errors: [{ field: "body.credential", message: "Credential must be plain text" }],
          },
        },
        422,
      ),
    );
    const { user, client } = setup({ routes: { [`PUT ${BASE}/hook_train/credential`]: put } });

    const train = await screen.findByRole("listitem", { name: "Train churn" });
    await user.type(within(train).getByLabelText("Credential"), SECRET);
    await user.click(within(train).getByRole("button", { name: "Set credential" }));

    expect(await screen.findByText("Some fields are missing or invalid.")).toBeInTheDocument();
    expect(screen.getByText("credential: Credential must be plain text")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(
      JSON.stringify(
        client
          .getMutationCache()
          .getAll()
          .map((m) => m.state),
      ),
    ).not.toContain(SECRET);
  });

  it("removes a credential only after a confirmation", async () => {
    const del = vi.fn(() => json({ ...SUSPENDED, credential_set: false }));
    const { user } = setup({ routes: { [`DELETE ${BASE}/hook_deploy/credential`]: del } });

    const deploy = await screen.findByRole("listitem", { name: "Deploy churn" });
    await user.click(within(deploy).getByRole("button", { name: "Remove credential" }));
    expect(del).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove credential" }));

    await waitFor(() => {
      expect(within(row("Deploy churn")).getByTestId("hook-credential")).toHaveTextContent(
        "No credential",
      );
    });
    expect(del).toHaveBeenCalledTimes(1);
  });

  it("offers no credential removal when none is set", async () => {
    setup();

    const train = await screen.findByRole("listitem", { name: "Train churn" });
    expect(within(train).queryByRole("button", { name: "Remove credential" })).toBeNull();
  });

  it("refetches when a hook.suspended event arrives", async () => {
    const { setHooks } = setup();
    await screen.findByRole("listitem", { name: "Train churn" });

    setHooks([{ ...ACTIVE, status: "suspended", suspended_reason: "Pins changed." }]);
    await act(async () => {
      stream.emit(
        "hook.suspended",
        {
          id: "evt_1",
          seq: 1,
          ts: "2026-10-30T10:00:00Z",
          source: "hooks",
          subject_id: "hook_train",
          payload: {},
          type: "hook.suspended",
        },
        1,
      );
      await Promise.resolve();
    });

    expect(await screen.findByText("Pins changed.")).toBeInTheDocument();
  });

  it("clears an old refusal when a later action on the hook succeeds", async () => {
    let refuse = true;
    const { user } = setup({
      routes: {
        [`PUT ${BASE}/hook_deploy/enabled`]: () =>
          refuse ? failure("Hook is busy.", 409) : json({ ...SUSPENDED, status: "disabled" }),
        [`POST ${BASE}/hook_deploy/approve`]: () =>
          json({ ...SUSPENDED, status: "active", suspended_reason: null }),
      },
    });

    const deploy = await screen.findByRole("listitem", { name: "Deploy churn" });
    await user.click(within(deploy).getByRole("button", { name: "Disable" }));
    expect(await screen.findByText("Hook is busy.")).toBeInTheDocument();
    refuse = false;
    await user.click(
      within(row("Deploy churn")).getByRole("button", { name: "Approve changed content" }),
    );

    await waitFor(() => {
      expect(screen.queryByText("Hook is busy.")).toBeNull();
    });
  });

  it.each(["proposal.decided", "handoff.approved"] as const)(
    "refetches when %s arrives",
    async (type) => {
      const { setHooks } = setup({ hooks: [ACTIVE] });
      await screen.findByRole("listitem", { name: "Train churn" });

      setHooks([ACTIVE, SUSPENDED]);
      await act(async () => {
        stream.emit(
          type,
          {
            id: "evt_1",
            seq: 1,
            ts: "2026-10-31T10:00:00Z",
            source: "proposals",
            subject_id: null,
            payload: {},
            type,
          },
          1,
        );
        await Promise.resolve();
      });

      expect(await screen.findByRole("listitem", { name: "Deploy churn" })).toBeInTheDocument();
    },
  );
});
