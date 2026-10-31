import { QueryClient } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { bodyField, failure, json, renderSection, stubRoutes } from "../project-settings/testing";
import { DiscordSection } from "./DiscordSection";

const ROUTE = "/api/integrations/discord";
// Obviously not a real webhook: the tests only need a value to look for in memory.
const FAKE_SECRET = "https://hooks.invalid/fake-secret-value";

interface Stored {
  enabled: boolean;
  webhook_set: boolean;
}

function setup(
  options: { stored?: Stored; putFails?: string; test?: Response; getFails?: boolean } = {},
) {
  let stored: Stored = options.stored ?? { enabled: false, webhook_set: false };
  let getFails = options.getFails ?? false;
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () => (getFails ? failure("Discord unavailable", 500) : json(stored)),
    [`PUT ${ROUTE}`]: (body) => {
      if (options.putFails) return failure(options.putFails, 422);
      const url = bodyField(body, "webhook_url");
      stored = {
        enabled: bodyField(body, "enabled") === true,
        webhook_set: typeof url === "string" ? true : stored.webhook_set,
      };
      return json(stored);
    },
    [`DELETE ${ROUTE}/webhook`]: () => {
      stored = { ...stored, webhook_set: false };
      return new Response(null, { status: 204 });
    },
    [`POST ${ROUTE}/test`]: () => options.test ?? json({ ok: true, detail: null }),
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  renderSection(<DiscordSection />, client);
  return {
    requests,
    client,
    user: userEvent.setup(),
    healLoads: () => {
      getFails = false;
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

it("explains waking as a mention that rings by the phone's settings", async () => {
  setup();
  await screen.findByLabelText("Discord webhook URL");
  expect(screen.getByText(/Waking someone means a Discord mention/)).toBeInTheDocument();
  expect(screen.getByText(/phone settings/)).toBeInTheDocument();
});

it("shows only that a webhook is set, never a value, and keeps the field empty", async () => {
  setup({ stored: { enabled: true, webhook_set: true } });
  const field = await screen.findByLabelText("Discord webhook URL");
  expect(field).toHaveValue("");
  expect(field).toHaveAttribute("type", "password");
  expect(screen.getByText("A webhook is set.")).toBeInTheDocument();
});

it("saves the webhook once, empties the field, and leaves no copy in memory", async () => {
  const { user, requests, client } = setup();
  const field = await screen.findByLabelText("Discord webhook URL");
  await user.type(field, FAKE_SECRET);
  await user.click(screen.getByRole("switch", { name: "Send notifications to Discord" }));
  await user.click(screen.getByRole("button", { name: "Save Discord" }));
  expect(await screen.findByText("Saved.")).toBeInTheDocument();

  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
    { enabled: true, webhook_url: FAKE_SECRET },
  ]);
  expect(screen.getByLabelText("Discord webhook URL")).toHaveValue("");
  expect(screen.getByText("A webhook is set.")).toBeInTheDocument();
  const held = JSON.stringify([
    client
      .getQueryCache()
      .getAll()
      .map((q) => q.state),
    client
      .getMutationCache()
      .getAll()
      .map((m) => m.state),
  ]);
  expect(held).not.toContain(FAKE_SECRET);
});

it("sends null, which keeps the stored webhook, when only the switch changes", async () => {
  const { user, requests } = setup({ stored: { enabled: true, webhook_set: true } });
  await user.click(await screen.findByRole("switch", { name: "Send notifications to Discord" }));
  await user.click(screen.getByRole("button", { name: "Save Discord" }));
  await screen.findByText("Saved.");
  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
    { enabled: false, webhook_url: null },
  ]);
});

it("shows the server's message for a rejected save and still empties the field", async () => {
  const { user } = setup({ putFails: "Discord webhook URL must use https." });
  const field = await screen.findByLabelText("Discord webhook URL");
  await user.type(field, FAKE_SECRET);
  await user.click(screen.getByRole("button", { name: "Save Discord" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Discord webhook URL must use https.");
  expect(screen.getByLabelText("Discord webhook URL")).toHaveValue("");
  expect(screen.queryByText(FAKE_SECRET)).not.toBeInTheDocument();
});

it("removes the stored webhook through the DELETE route", async () => {
  const { user, requests } = setup({ stored: { enabled: false, webhook_set: true } });
  await user.click(await screen.findByRole("button", { name: "Remove webhook" }));
  await waitFor(() => {
    expect(screen.getByText("No webhook is set.")).toBeInTheDocument();
  });
  expect(requests.some((r) => r.method === "DELETE" && r.path === `${ROUTE}/webhook`)).toBe(true);
});

it("sends a test to the typed Discord user and reports success", async () => {
  const { user, requests } = setup();
  await user.type(await screen.findByLabelText("Discord user ID to mention"), "12345");
  await user.click(screen.getByRole("button", { name: "Send test to Discord" }));
  expect(await screen.findByText("Test message sent.")).toBeInTheDocument();
  expect(requests.filter((r) => r.path === `${ROUTE}/test`).map((r) => r.body)).toEqual([
    { target: "12345" },
  ]);
});

it("shows the server's scrubbed text when the test fails", async () => {
  const { user } = setup({ test: json({ ok: false, detail: "Discord answered 404" }) });
  await user.type(await screen.findByLabelText("Discord user ID to mention"), "12345");
  await user.click(screen.getByRole("button", { name: "Send test to Discord" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Discord answered 404");
});

it("shows the server's error when the test request itself fails", async () => {
  const { user } = setup({ test: failure("Test unavailable", 500) });
  await user.type(await screen.findByLabelText("Discord user ID to mention"), "12345");
  await user.click(screen.getByRole("button", { name: "Send test to Discord" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Test unavailable");
});

it("offers a retry after a failed load", async () => {
  const { user, healLoads } = setup({ getFails: true });
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Discord unavailable");
  healLoads();
  await user.click(within(alert.parentElement ?? alert).getByRole("button", { name: "Retry" }));
  expect(await screen.findByLabelText("Discord webhook URL")).toBeInTheDocument();
});
