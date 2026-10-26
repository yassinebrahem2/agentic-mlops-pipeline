import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { IssuesSection } from "./IssuesSection";
import { failure, json, renderSection, stubRoutes } from "./testing";

type Priority = components["schemas"]["IssuesSettings"]["min_priority"];

const ROUTE = "/api/projects/prj_demo/settings/issues";

function setup(options: { priority?: Priority; getFails?: boolean; putFails?: boolean } = {}) {
  let priority: Priority = options.priority ?? "medium";
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () =>
      options.getFails
        ? failure("Issue settings unavailable", 500)
        : json({ min_priority: priority }),
    [`PUT ${ROUTE}`]: (body) => {
      if (options.putFails) return failure("Priority not saved");
      priority = (body as { min_priority: Priority }).min_priority;
      return json({ min_priority: priority });
    },
  });
  renderSection(<IssuesSection projectId="prj_demo" />);
  return { requests, user: userEvent.setup() };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

it("loads the stored minimum priority and offers the four priorities", async () => {
  const { user } = setup({ priority: "high" });
  const select = await screen.findByRole("combobox", { name: "Minimum priority" });
  expect(select).toHaveTextContent("High");
  await user.click(select);
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
    "Low",
    "Medium",
    "High",
    "Critical",
  ]);
});

it("saves the chosen priority", async () => {
  const { user, requests } = setup();
  const select = await screen.findByRole("combobox", { name: "Minimum priority" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "Critical" }));
  await waitFor(() => {
    expect(select).toHaveTextContent("Critical");
  });
  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
    { min_priority: "critical" },
  ]);
});

it("shows the server's error for a failed load", async () => {
  setup({ getFails: true });
  expect(await screen.findByRole("alert")).toHaveTextContent("Issue settings unavailable");
});

it("shows the server's error for a failed save and keeps the stored priority", async () => {
  const { user } = setup({ putFails: true });
  const select = await screen.findByRole("combobox", { name: "Minimum priority" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "Low" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Priority not saved");
  expect(select).toHaveTextContent("Medium");
});
