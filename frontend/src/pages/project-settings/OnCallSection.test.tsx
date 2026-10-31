import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { OnCallSection } from "./OnCallSection";
import { failure, json, renderSection, stubRoutes } from "./testing";

type Person = components["schemas"]["OnCallPerson"];
type OnCall = components["schemas"]["OnCall"];

const ROUTE = "/api/projects/prj_demo/on-call";

const ADA: Person = {
  id: "ada",
  name: "Ada",
  contacts: [{ channel: "discord", address: "111" }],
  hours: { days: ["mon", "tue"], start: "09:00", end: "17:00" },
  timezone: "Europe/Paris",
  escalation_order: 1,
};
const BEN: Person = {
  id: "ben",
  name: "Ben",
  contacts: [{ channel: "email", address: "ben@example.test" }],
  hours: { days: ["sat"], start: "22:00", end: "06:00" },
  timezone: "Asia/Tokyo",
  escalation_order: 2,
};

function withoutId(person: Person): OnCall {
  return {
    name: person.name,
    contacts: person.contacts,
    hours: person.hours,
    timezone: person.timezone,
    escalation_order: person.escalation_order,
  };
}

function setup(options: { people?: Person[]; put?: Response; getFails?: boolean } = {}) {
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () =>
      options.getFails ? failure("On-call unavailable", 500) : json(options.people ?? [ADA, BEN]),
    [`PUT ${ROUTE}`]: (body) => options.put ?? json(body),
  });
  renderSection(<OnCallSection projectId="prj_demo" />);
  return { requests, user: userEvent.setup() };
}

function puts(requests: { method: string; body: unknown }[]): unknown[] {
  return requests.filter((r) => r.method === "PUT").map((r) => r.body);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

it("lists the people in escalation order with their details", async () => {
  setup();
  const first = await screen.findByRole("group", { name: "Person 1" });
  expect(within(first).getByLabelText("Name")).toHaveValue("Ada");
  expect(within(first).getByLabelText("Time zone")).toHaveValue("Europe/Paris");
  expect(within(first).getByLabelText("Start")).toHaveValue("09:00");
  expect(within(first).getByLabelText("End")).toHaveValue("17:00");
  expect(within(first).getByRole("checkbox", { name: "Mon" })).toBeChecked();
  expect(within(first).getByRole("checkbox", { name: "Wed" })).not.toBeChecked();
  expect(within(first).getByLabelText("Contact 1 address")).toHaveValue("111");
  const second = screen.getByRole("group", { name: "Person 2" });
  expect(within(second).getByLabelText("Name")).toHaveValue("Ben");
});

it("says a newly approved handoff replaces the list", async () => {
  setup();
  expect(await screen.findByText(/newly approved handoff replaces/)).toBeInTheDocument();
});

it("says so when nobody is on call yet", async () => {
  setup({ people: [] });
  expect(await screen.findByText("Nobody is on call yet.")).toBeInTheDocument();
});

it("saves the whole list unchanged, without ids", async () => {
  const { user, requests } = setup();
  const first = await screen.findByRole("group", { name: "Person 1" });
  await user.type(within(first).getByLabelText("Name"), "!");
  await user.click(screen.getByRole("button", { name: "Save on-call" }));
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
  expect(puts(requests)).toEqual([[{ ...withoutId(ADA), name: "Ada!" }, withoutId(BEN)]]);
});

it("adds a person and sends the full list, numbered by position", async () => {
  const { user, requests } = setup({ people: [ADA] });
  await user.click(await screen.findByRole("button", { name: "Add person" }));
  const added = screen.getByRole("group", { name: "Person 2" });
  await user.type(within(added).getByLabelText("Name"), "Cy");
  await user.clear(within(added).getByLabelText("Time zone"));
  await user.type(within(added).getByLabelText("Time zone"), "UTC");
  await user.type(within(added).getByLabelText("Contact 1 address"), "222");
  await user.click(screen.getByRole("button", { name: "Save on-call" }));
  await screen.findByText("Saved.");
  const sent = puts(requests)[0] as OnCall[];
  expect(sent).toHaveLength(2);
  expect(sent[0]).toEqual(withoutId(ADA));
  expect(sent[1]).toMatchObject({
    name: "Cy",
    timezone: "UTC",
    escalation_order: 2,
    contacts: [{ channel: "discord", address: "222" }],
  });
});

it("removes a person and renumbers the rest", async () => {
  const { user, requests } = setup();
  const first = await screen.findByRole("group", { name: "Person 1" });
  await user.click(within(first).getByRole("button", { name: "Remove person 1" }));
  await user.click(screen.getByRole("button", { name: "Save on-call" }));
  await screen.findByText("Saved.");
  expect(puts(requests)).toEqual([[{ ...withoutId(BEN), escalation_order: 1 }]]);
});

it("edits hours, days, channel and contacts", async () => {
  const { user, requests } = setup({ people: [ADA] });
  const person = await screen.findByRole("group", { name: "Person 1" });
  await user.click(within(person).getByRole("checkbox", { name: "Wed" }));
  await user.click(within(person).getByRole("checkbox", { name: "Mon" }));
  fireEvent.change(within(person).getByLabelText("Start"), { target: { value: "08:30" } });
  await user.selectOptions(within(person).getByLabelText("Contact 1 channel"), "email");
  await user.click(within(person).getByRole("button", { name: "Add contact" }));
  await user.type(within(person).getByLabelText("Contact 2 address"), "x@y.test");
  await user.click(screen.getByRole("button", { name: "Save on-call" }));
  await screen.findByText("Saved.");
  expect(puts(requests)).toEqual([
    [
      {
        ...withoutId(ADA),
        hours: { days: ["tue", "wed"], start: "08:30", end: "17:00" },
        contacts: [
          { channel: "email", address: "111" },
          { channel: "discord", address: "x@y.test" },
        ],
      },
    ],
  ]);
});

it("reorders people and numbers the escalation order by the new position", async () => {
  const { user, requests } = setup();
  const second = await screen.findByRole("group", { name: "Person 2" });
  await user.click(within(second).getByRole("button", { name: "Move person 2 up" }));
  await user.click(screen.getByRole("button", { name: "Save on-call" }));
  await screen.findByText("Saved.");
  expect(puts(requests)).toEqual([
    [
      { ...withoutId(BEN), escalation_order: 1 },
      { ...withoutId(ADA), escalation_order: 2 },
    ],
  ]);
});

it("disables the first Move up and the last Move down", async () => {
  setup();
  const first = await screen.findByRole("group", { name: "Person 1" });
  const second = screen.getByRole("group", { name: "Person 2" });
  expect(within(first).getByRole("button", { name: "Move person 1 up" })).toBeDisabled();
  expect(within(second).getByRole("button", { name: "Move person 2 down" })).toBeDisabled();
});

it("shows the 422 message and the failing field", async () => {
  const { user } = setup({
    put: json(
      {
        code: "invalid_request",
        message: "Some fields are missing or invalid.",
        detail: {
          errors: [
            { field: "body.0.timezone", message: "'Mars/Base' is not an IANA time zone name" },
          ],
        },
      },
      422,
    ),
  });
  const first = await screen.findByRole("group", { name: "Person 1" });
  await user.type(within(first).getByLabelText("Time zone"), "x");
  await user.click(screen.getByRole("button", { name: "Save on-call" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Some fields are missing or invalid.");
  expect(first).toHaveTextContent("timezone: 'Mars/Base' is not an IANA time zone name");
});

it("shows the server's error for a failed load, with a Retry", async () => {
  setup({ getFails: true });
  expect(await screen.findByRole("alert")).toHaveTextContent("On-call unavailable");
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
