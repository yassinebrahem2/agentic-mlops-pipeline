import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SignIn } from "./SignIn";

describe("SignIn component", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client: queryClient }, children);
  }

  it("renders local mode message without token error", () => {
    render(<SignIn mode="local" />, { wrapper });

    expect(
      screen.getByText("Open the app with the link printed in the terminal where it runs."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });

  it("renders local mode message with token error above instructions", () => {
    const errorMsg = "The access link is invalid or has expired.";
    render(<SignIn mode="local" tokenError={errorMsg} />, { wrapper });

    const errorEl = screen.getByText(errorMsg);
    const instructionEl = screen.getByText(
      "Open the app with the link printed in the terminal where it runs.",
    );

    expect(errorEl).toBeInTheDocument();
    expect(instructionEl).toBeInTheDocument();

    // Verify error message is rendered above the instructions in DOM order
    expect(
      errorEl.compareDocumentPosition(instructionEl) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders server mode with password form", () => {
    render(<SignIn mode="server" />, { wrapper });

    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("handles wrong password with error message in server mode", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/login") && req.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "bad_password",
              message: "Wrong password.",
            }),
            {
              status: 401,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      return Promise.reject(new Error(`Unexpected request to ${req.url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    render(<SignIn mode="server" />, { wrapper });

    await user.type(screen.getByLabelText("Password"), "wrongpass");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByText("Wrong password.")).toBeInTheDocument();
    });
  });

  it("handles correct password submission in server mode", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/login") && req.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              mode: "server",
              signed_in: true,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      return Promise.reject(new Error(`Unexpected request to ${req.url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    render(<SignIn mode="server" />, { wrapper });

    await user.type(screen.getByLabelText("Password"), "correctpass");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(queryClient.getQueryData(["auth"])).toEqual({
        mode: "server",
        signed_in: true,
      });
    });
  });
});
