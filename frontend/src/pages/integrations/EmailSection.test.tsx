import { QueryClient } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { bodyField, failure, json, renderSection, stubRoutes } from "../project-settings/testing";
import { EmailSection } from "./EmailSection";

const ROUTE = "/api/integrations/email";
// Obviously not a real password: the tests only need a value to look for in memory.
const FAKE_SECRET = "not-a-real-smtp-password";

interface Stored {
  enabled: boolean;
  host: string;
  port: number;
  user: string;
  sender: string;
  starttls: boolean;
  password_set: boolean;
}

const EMPTY: Stored = {
  enabled: false,
  host: "",
  port: 25,
  user: "",
  sender: "",
  starttls: false,
  password_set: false,
};

function setup(
  options: { stored?: Stored; put?: (body: unknown) => Response; test?: Response } = {},
) {
  let stored = options.stored ?? EMPTY;
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () => json(stored),
    [`PUT ${ROUTE}`]: (body) => {
      if (options.put) return options.put(body);
      stored = {
        enabled: bodyField(body, "enabled") === true,
        host: String(bodyField(body, "host")),
        port: Number(bodyField(body, "port")),
        user: String(bodyField(body, "user")),
        sender: String(bodyField(body, "sender")),
        starttls: bodyField(body, "starttls") === true,
        password_set: typeof bodyField(body, "password") === "string" || stored.password_set,
      };
      return json(stored);
    },
    [`DELETE ${ROUTE}/password`]: () => {
      stored = { ...stored, password_set: false };
      return new Response(null, { status: 204 });
    },
    [`POST ${ROUTE}/test`]: () => options.test ?? json({ ok: true, detail: null }),
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  renderSection(<EmailSection />, client);
  return { requests, client, user: userEvent.setup() };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

it("explains waking as an urgent email that rings by the phone's settings", async () => {
  setup();
  await screen.findByLabelText("SMTP host");
  expect(screen.getByText(/urgent email/)).toBeInTheDocument();
  expect(screen.getByText(/phone settings/)).toBeInTheDocument();
});

it("loads the stored server settings into labelled fields", async () => {
  setup({
    stored: { ...EMPTY, host: "smtp.example.test", port: 2525, user: "robot", sender: "a@b.test" },
  });
  expect(await screen.findByLabelText("SMTP host")).toHaveValue("smtp.example.test");
  expect(screen.getByLabelText("Port")).toHaveValue(2525);
  expect(screen.getByLabelText("SMTP user")).toHaveValue("robot");
  expect(screen.getByLabelText("Sender address")).toHaveValue("a@b.test");
  expect(screen.getByLabelText("SMTP password")).toHaveAttribute("type", "password");
  expect(screen.getByLabelText("SMTP password")).toHaveValue("");
});

it("saves the settings and the password once, then leaves no copy anywhere", async () => {
  const { user, requests, client } = setup();
  await user.type(await screen.findByLabelText("SMTP host"), "smtp.example.test");
  await user.clear(screen.getByLabelText("Port"));
  await user.type(screen.getByLabelText("Port"), "2525");
  await user.type(screen.getByLabelText("SMTP user"), "robot");
  await user.type(screen.getByLabelText("Sender address"), "a@b.test");
  await user.type(screen.getByLabelText("SMTP password"), FAKE_SECRET);
  await user.click(screen.getByRole("switch", { name: "Use STARTTLS" }));
  await user.click(screen.getByRole("switch", { name: "Send notifications by email" }));
  await user.click(screen.getByRole("button", { name: "Save email" }));
  expect(await screen.findByText("Saved.")).toBeInTheDocument();

  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
    {
      enabled: true,
      host: "smtp.example.test",
      port: 2525,
      user: "robot",
      sender: "a@b.test",
      starttls: true,
      password: FAKE_SECRET,
    },
  ]);
  expect(screen.getByLabelText("SMTP password")).toHaveValue("");
  expect(screen.getByText("A password is set.")).toBeInTheDocument();
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

it("sends a null password, keeping the stored one, when the field is empty", async () => {
  const { user, requests } = setup({ stored: { ...EMPTY, host: "h", password_set: true } });
  await user.click(await screen.findByRole("switch", { name: "Use STARTTLS" }));
  await user.click(screen.getByRole("button", { name: "Save email" }));
  await screen.findByText("Saved.");
  expect(bodyField(requests.find((r) => r.method === "PUT")?.body, "password")).toBeNull();
});

it("shows the server's message and the failing field for a rejected save", async () => {
  const { user } = setup({
    put: () =>
      json(
        {
          code: "invalid_request",
          message: "Some fields are missing or invalid.",
          detail: { errors: [{ field: "body.port", message: "Input should be at most 65535" }] },
        },
        422,
      ),
  });
  await user.click(await screen.findByRole("switch", { name: "Use STARTTLS" }));
  await user.click(screen.getByRole("button", { name: "Save email" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Some fields are missing or invalid.");
  expect(screen.getByText("Input should be at most 65535")).toBeInTheDocument();
});

it("shows the server's message when email is enabled without a host", async () => {
  const { user } = setup({
    put: () => failure("Email host and sender are required when email is enabled.", 422),
  });
  await user.click(await screen.findByRole("switch", { name: "Send notifications by email" }));
  await user.click(screen.getByRole("button", { name: "Save email" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Email host and sender are required when email is enabled.",
  );
});

it("removes the stored password through the DELETE route", async () => {
  const { user, requests } = setup({ stored: { ...EMPTY, password_set: true } });
  await user.click(await screen.findByRole("button", { name: "Remove password" }));
  await waitFor(() => {
    expect(screen.getByText("No password is set.")).toBeInTheDocument();
  });
  expect(requests.some((r) => r.method === "DELETE" && r.path === `${ROUTE}/password`)).toBe(true);
});

it("sends a test to the typed address and shows the server's text when it fails", async () => {
  const { user, requests } = setup({ test: json({ ok: false, detail: "Connection refused" }) });
  await user.type(await screen.findByLabelText("Address to send the test to"), "me@b.test");
  await user.click(screen.getByRole("button", { name: "Send test email" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Connection refused");
  expect(requests.filter((r) => r.path === `${ROUTE}/test`).map((r) => r.body)).toEqual([
    { target: "me@b.test" },
  ]);
});
