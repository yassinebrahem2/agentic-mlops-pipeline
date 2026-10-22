import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";
import { APP_NAME } from "./config";
import { queryClient } from "./lib/query";

afterEach(() => {
  queryClient.clear();
  vi.unstubAllGlobals();
});

test("renders the app with APP_NAME and the Projects page at root", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes("/api/auth/status")) {
        return new Response(JSON.stringify({ mode: "local", signed_in: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/projects")) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/app")) {
        return new Response(JSON.stringify({ name: APP_NAME, mode: "local", version: "1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(null, { status: 404 });
    }),
  );

  render(<App />);
  expect(await screen.findByRole("heading", { name: APP_NAME })).toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: "Projects" })).toBeInTheDocument();
});
