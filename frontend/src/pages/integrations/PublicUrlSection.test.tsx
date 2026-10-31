import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { failure, json, renderSection, stubRoutes } from "../project-settings/testing";
import { PublicUrlSection } from "./PublicUrlSection";

const ROUTE = "/api/integrations/public-url";

function setup(options: { url?: string | null; putFails?: string } = {}) {
  let url = options.url ?? null;
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () => json({ public_url: url }),
    [`PUT ${ROUTE}`]: (body) => {
      if (options.putFails) return failure(options.putFails, 422);
      const next =
        body !== null && typeof body === "object" && "public_url" in body ? body.public_url : null;
      url = typeof next === "string" ? next : null;
      return json({ public_url: url });
    },
  });
  renderSection(<PublicUrlSection />);
  return { requests, user: userEvent.setup() };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

it("says links cannot be opened from a phone and messages carry the full summary", async () => {
  setup({ url: null });
  await screen.findByLabelText("Public URL");
  expect(screen.getByText(/cannot be opened from a phone/)).toBeInTheDocument();
  expect(screen.getByText(/full summary/)).toBeInTheDocument();
});

it("does not show the local-mode note when a public URL is set", async () => {
  setup({ url: "https://app.example.test" });
  expect(await screen.findByLabelText("Public URL")).toHaveValue("https://app.example.test");
  expect(screen.queryByText(/cannot be opened from a phone/)).not.toBeInTheDocument();
});

it("saves the typed address", async () => {
  const { user, requests } = setup();
  await user.type(await screen.findByLabelText("Public URL"), "https://app.example.test");
  await user.click(screen.getByRole("button", { name: "Save public URL" }));
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
    { public_url: "https://app.example.test" },
  ]);
});

it("clears the public URL by sending null when the field is emptied", async () => {
  const { user, requests } = setup({ url: "https://app.example.test" });
  await user.clear(await screen.findByLabelText("Public URL"));
  await user.click(screen.getByRole("button", { name: "Save public URL" }));
  expect(await screen.findByText(/cannot be opened from a phone/)).toBeInTheDocument();
  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual([
    { public_url: null },
  ]);
});

it("shows the server's message for a rejected address", async () => {
  const { user } = setup({ putFails: "public_url must start with https://" });
  await user.type(await screen.findByLabelText("Public URL"), "http://x");
  await user.click(screen.getByRole("button", { name: "Save public URL" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("public_url must start with https://");
});
