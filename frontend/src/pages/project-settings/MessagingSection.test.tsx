import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { MessagingSection } from "./MessagingSection";
import { failure, json, renderSection, stubRoutes } from "./testing";

type Limits = components["schemas"]["MessagingLimits"];

const ROUTE = "/api/projects/prj_demo/settings/messaging";

function isLimits(body: unknown): body is Limits {
  return (
    body !== null &&
    typeof body === "object" &&
    "max_hops" in body &&
    typeof body.max_hops === "number" &&
    "max_tokens" in body &&
    typeof body.max_tokens === "number"
  );
}

function setup(options: { getFails?: boolean; putFails?: boolean } = {}) {
  let limits: Limits = { max_hops: 6, max_tokens: 300000 };
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () =>
      options.getFails ? failure("Message limits unavailable", 500) : json(limits),
    [`PUT ${ROUTE}`]: (body) => {
      if (options.putFails) return failure("Limits not saved");
      if (isLimits(body)) limits = body;
      return json(limits);
    },
  });
  renderSection(<MessagingSection projectId="prj_demo" />);
  return { requests, user: userEvent.setup() };
}

const puts = (requests: { method: string; body: unknown }[]) =>
  requests.filter((r) => r.method === "PUT").map((r) => r.body);

afterEach(() => {
  vi.unstubAllGlobals();
});

it("loads the stored limits and describes what each one limits", async () => {
  setup();
  expect(await screen.findByRole("spinbutton", { name: "Maximum messages" })).toHaveValue(6);
  expect(screen.getByRole("spinbutton", { name: "Maximum tokens" })).toHaveValue(300000);
  expect(screen.getByRole("region", { name: "Agent messages" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  expect(screen.getByText(/most messages one exchange/i)).toBeInTheDocument();
  expect(screen.getByText(/most model tokens/i)).toBeInTheDocument();
});

it("saves both limits as numbers and shows the saved values", async () => {
  const { user, requests } = setup();
  const hops = await screen.findByRole("spinbutton", { name: "Maximum messages" });
  await user.clear(hops);
  await user.type(hops, "10");
  const tokens = screen.getByRole("spinbutton", { name: "Maximum tokens" });
  await user.clear(tokens);
  await user.type(tokens, "50000");
  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => {
    expect(puts(requests)).toEqual([{ max_hops: 10, max_tokens: 50000 }]);
  });
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
  expect(hops).toHaveValue(10);
  expect(tokens).toHaveValue(50000);
});

it("treats a differently written stored value as unchanged", async () => {
  const { user } = setup();
  const hops = await screen.findByRole("spinbutton", { name: "Maximum messages" });
  await user.clear(hops);
  await user.type(hops, "06");
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
});

it.each([
  ["zero", "0"],
  ["a negative number", "-3"],
  ["a fractional number", "2.5"],
  ["nothing", ""],
])("rejects %s without sending a request", async (_name, text) => {
  const { user, requests } = setup();
  const hops = await screen.findByRole("spinbutton", { name: "Maximum messages" });
  await user.clear(hops);
  if (text !== "") await user.type(hops, text);
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("Enter a whole number of at least 1.")).toBeInTheDocument();
  expect(hops).toHaveAttribute("aria-invalid", "true");
  expect(puts(requests)).toEqual([]);
});

it("rejects an invalid token limit without sending a request", async () => {
  const { user, requests } = setup();
  const tokens = await screen.findByRole("spinbutton", { name: "Maximum tokens" });
  await user.clear(tokens);
  await user.type(tokens, "0");
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("Enter a whole number of at least 1.")).toBeInTheDocument();
  expect(tokens).toHaveAttribute("aria-invalid", "true");
  expect(puts(requests)).toEqual([]);
});

it("shows the server's error for a failed load", async () => {
  setup({ getFails: true });
  expect(await screen.findByRole("alert")).toHaveTextContent("Message limits unavailable");
});

it("shows the server's error for a failed save and keeps what was typed", async () => {
  const { user } = setup({ putFails: true });
  const hops = await screen.findByRole("spinbutton", { name: "Maximum messages" });
  await user.clear(hops);
  await user.type(hops, "12");
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Limits not saved");
  expect(hops).toHaveValue(12);
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
});
