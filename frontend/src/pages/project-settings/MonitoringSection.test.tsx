import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { MonitoringSection } from "./MonitoringSection";
import { failure, json, renderSection, stubRoutes } from "./testing";

type Settings = components["schemas"]["MonitoringSettings"];

const ROUTE = "/api/projects/prj_demo/monitoring-settings";

const STORED: Settings = {
  collection_interval_s: 61,
  grouping_window_minutes: 62,
  reopen_window_hours: 63,
  recent_change_window_hours: 64,
  escalation_timeout_minutes: 65,
  check_grace_minutes: 66,
  second_deadline_minutes: 67,
  parser_disable_after: 68,
  evaluation_max_age_hours: 69,
  circuit_breaker_fixes: 70,
  circuit_breaker_window_days: 71,
  timezone: null,
};

const LABELS: [keyof Settings, string][] = [
  ["collection_interval_s", "Collection interval (seconds)"],
  ["grouping_window_minutes", "Grouping window (minutes)"],
  ["reopen_window_hours", "Reopen window (hours)"],
  ["recent_change_window_hours", "Recent change window (hours)"],
  ["escalation_timeout_minutes", "Escalation timeout (minutes)"],
  ["check_grace_minutes", "Check grace (minutes)"],
  ["second_deadline_minutes", "Second deadline (minutes)"],
  ["parser_disable_after", "Disable a parser after (failures)"],
  ["evaluation_max_age_hours", "Evaluation maximum age (hours)"],
  ["circuit_breaker_fixes", "Circuit breaker fixes"],
  ["circuit_breaker_window_days", "Circuit breaker window (days)"],
];

function setup(options: { stored?: Settings; put?: Response; getFails?: boolean } = {}) {
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () =>
      options.getFails ? failure("Settings unavailable", 500) : json(options.stored ?? STORED),
    [`PUT ${ROUTE}`]: (body) => options.put ?? json(body),
  });
  renderSection(<MonitoringSection projectId="prj_demo" />);
  return { requests, user: userEvent.setup() };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

it("shows every stored setting in a labelled field", async () => {
  setup();
  for (const [name, label] of LABELS) {
    expect(await screen.findByLabelText(label)).toHaveValue(Number(STORED[name]));
  }
  expect(screen.getByLabelText("Time zone")).toHaveValue("");
  expect(screen.getByText(/empty.*server's/)).toBeInTheDocument();
});

it("saves every field, with an empty time zone as null", async () => {
  const { user, requests } = setup();
  const interval = await screen.findByLabelText("Collection interval (seconds)");
  await user.clear(interval);
  await user.type(interval, "30");
  await user.click(screen.getByRole("button", { name: "Save monitoring" }));
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
    { ...STORED, collection_interval_s: 30, timezone: null },
  ]);
});

it("sends a typed time zone as is", async () => {
  const { user, requests } = setup();
  await user.type(await screen.findByLabelText("Time zone"), "Europe/Paris");
  await user.click(screen.getByRole("button", { name: "Save monitoring" }));
  await screen.findByText("Saved.");
  expect(requests.find((r) => r.method === "PUT")?.body).toMatchObject({
    timezone: "Europe/Paris",
  });
});

it("shows the 422 message and the failing field as returned", async () => {
  const { user } = setup({
    put: json(
      {
        code: "invalid_request",
        message: "Some fields are missing or invalid.",
        detail: {
          errors: [
            {
              field: "body.collection_interval_s",
              message: "collection_interval_s must be at least 10, got 5",
            },
          ],
        },
      },
      422,
    ),
  });
  const interval = await screen.findByLabelText("Collection interval (seconds)");
  await user.clear(interval);
  await user.type(interval, "5");
  await user.click(screen.getByRole("button", { name: "Save monitoring" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Some fields are missing or invalid.");
  expect(screen.getByText("collection_interval_s must be at least 10, got 5")).toBeInTheDocument();
  expect(interval).toHaveAttribute("aria-invalid", "true");
});

it("does not send a field that is not a whole number", async () => {
  const { user, requests } = setup();
  const interval = await screen.findByLabelText("Collection interval (seconds)");
  await user.clear(interval);
  await user.click(screen.getByRole("button", { name: "Save monitoring" }));
  expect(await screen.findByText("Enter a whole number.")).toBeInTheDocument();
  expect(requests.some((r) => r.method === "PUT")).toBe(false);
});

it("keeps Save disabled until something changes", async () => {
  setup();
  expect(await screen.findByRole("button", { name: "Save monitoring" })).toBeDisabled();
});

it("shows the server's error for a failed load, with a Retry", async () => {
  setup({ getFails: true });
  expect(await screen.findByRole("alert")).toHaveTextContent("Settings unavailable");
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
});

it("points to the issue threshold instead of repeating it", async () => {
  setup();
  expect(await screen.findByText(/GitHub issues section/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Minimum priority")).not.toBeInTheDocument();
});
