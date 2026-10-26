import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { PermissionsSection } from "./PermissionsSection";
import { failure, json, renderSection, stubRoutes } from "./testing";

type Level = components["schemas"]["PermissionsSettings"]["level"];

const ROUTE = "/api/projects/prj_demo/settings/permissions";

function setup(options: { level?: Level; getFails?: boolean; putFails?: boolean } = {}) {
  let level: Level = options.level ?? "ask";
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () =>
      options.getFails ? failure("Permissions unavailable", 500) : json({ level }),
    [`PUT ${ROUTE}`]: (body) => {
      if (options.putFails) return failure("Level not saved");
      level = (body as { level: Level }).level;
      return json({ level });
    },
  });
  renderSection(<PermissionsSection projectId="prj_demo" />);
  return { requests, user: userEvent.setup() };
}
const puts = (requests: { method: string; body: unknown }[]) =>
  requests.filter((r) => r.method === "PUT").map((r) => r.body);

afterEach(() => {
  vi.unstubAllGlobals();
});

it("loads the stored level and explains every level in one line", async () => {
  setup({ level: "autonomous" });
  expect(await screen.findByRole("combobox", { name: "Permission level" })).toHaveTextContent(
    "Autonomous",
  );
  const list = screen.getByRole("list", { name: "What each level allows" });
  const items = within(list).getAllByRole("listitem");
  expect(items.map((item) => item.querySelector("strong")?.textContent)).toEqual([
    "Suggest",
    "Ask",
    "Autonomous",
    "Bypass",
  ]);
  for (const item of items) {
    const label = item.querySelector("strong")?.textContent ?? "";
    expect(item.textContent.length).toBeGreaterThan(label.length + 20);
  }
});

it("saves a level chosen from the list", async () => {
  const { user, requests } = setup();
  const select = await screen.findByRole("combobox", { name: "Permission level" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "Suggest" }));
  await waitFor(() => {
    expect(select).toHaveTextContent("Suggest");
  });
  expect(puts(requests)).toEqual([{ level: "suggest" }]);
});

it("needs a confirmation before saving bypass, and Cancel saves nothing", async () => {
  const { user, requests } = setup();
  const select = await screen.findByRole("combobox", { name: "Permission level" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "Bypass" }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent("without asking");
  expect(dialog).toHaveTextContent("sandbox");
  expect(puts(requests)).toEqual([]);
  await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  expect(select).toHaveTextContent("Ask");
  expect(puts(requests)).toEqual([]);
});

it("saves bypass once the confirmation is accepted", async () => {
  const { user, requests } = setup();
  const select = await screen.findByRole("combobox", { name: "Permission level" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "Bypass" }));
  await user.click(
    within(await screen.findByRole("dialog")).getByRole("button", { name: "Allow bypass" }),
  );
  await waitFor(() => {
    expect(select).toHaveTextContent("Bypass");
  });
  expect(puts(requests)).toEqual([{ level: "bypass" }]);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("shows the server's error for a failed load", async () => {
  setup({ getFails: true });
  expect(await screen.findByRole("alert")).toHaveTextContent("Permissions unavailable");
});

it("shows the server's error for a failed save and keeps the stored level", async () => {
  const { user } = setup({ putFails: true });
  const select = await screen.findByRole("combobox", { name: "Permission level" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "Autonomous" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Level not saved");
  expect(select).toHaveTextContent("Ask");
});

it("shows the server's error and the stored level when a confirmed bypass fails", async () => {
  const { user } = setup({ putFails: true });
  const select = await screen.findByRole("combobox", { name: "Permission level" });
  await user.click(select);
  await user.click(await screen.findByRole("option", { name: "Bypass" }));
  await user.click(
    within(await screen.findByRole("dialog")).getByRole("button", { name: "Allow bypass" }),
  );
  expect(await screen.findByText("Level not saved")).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  expect(select).toHaveTextContent("Ask");
});
