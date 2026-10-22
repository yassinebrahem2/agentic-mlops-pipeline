import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGate } from "./AuthGate";
import { resetTokenExchange } from "./useAuth";

describe("AuthGate token handling", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    resetTokenExchange();
    window.location.hash = "";
    history.replaceState(null, "", "/");
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
    window.location.hash = "";
    history.replaceState(null, "", "/");
  });

  function renderWithClient(ui: React.ReactElement) {
    return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  }

  it("#token=abc posts token exactly once in StrictMode, removes hash, and shows shell", async () => {
    window.location.hash = "#token=abc";

    const tokenPostBodies: unknown[] = [];
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const req = new Request(input, init);
        if (req.url.includes("/api/auth/token") && req.method === "POST") {
          const bodyText = await req.clone().text();
          tokenPostBodies.push(JSON.parse(bodyText || "{}"));
          return new Response(
            JSON.stringify({
              mode: "local",
              signed_in: true,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        if (req.url.includes("/api/auth/status")) {
          return new Response(
            JSON.stringify({
              mode: "local",
              signed_in: true,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        return Promise.reject(new Error(`Unexpected request to ${req.url}`));
      });
    vi.stubGlobal("fetch", fakeFetch);

    renderWithClient(
      <StrictMode>
        <AuthGate>
          <div>The App Shell Content</div>
        </AuthGate>
      </StrictMode>,
    );

    await waitFor(() => {
      expect(screen.getByText("The App Shell Content")).toBeInTheDocument();
    });

    // Exactly once in StrictMode
    expect(tokenPostBodies).toEqual([{ token: "abc" }]);

    // Hash removed
    expect(window.location.hash).toBe("");
  });

  it("rejected token shows bad_token message and local-mode instructions", async () => {
    window.location.hash = "#token=expired_token";

    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/token") && req.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "bad_token",
              message:
                "This link is out of date. Open the address printed in the terminal where the app runs.",
            }),
            {
              status: 401,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      if (req.url.includes("/api/auth/status")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              mode: "local",
              signed_in: false,
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

    renderWithClient(
      <AuthGate>
        <div>The App Shell Content</div>
      </AuthGate>,
    );

    await waitFor(() => {
      expect(
        screen.getByText(
          "This link is out of date. Open the address printed in the terminal where the app runs.",
        ),
      ).toBeInTheDocument();
    });

    expect(
      screen.getByText("Open the app with the link printed in the terminal where it runs."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The App Shell Content")).not.toBeInTheDocument();
  });

  it("token never appears in history after exchange and replaceState receives existing history.state", async () => {
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    const initialHistoryState = { page: "home", scrollPos: 120 };
    window.history.replaceState(
      initialHistoryState,
      "",
      "/workspace/app?query=active#token=confidential_token",
    );
    replaceStateSpy.mockClear();

    expect(window.location.href).toContain("confidential_token");

    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/token") && req.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              mode: "local",
              signed_in: true,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      if (req.url.includes("/api/auth/status")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              mode: "local",
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

    renderWithClient(
      <AuthGate>
        <div>The App Shell Content</div>
      </AuthGate>,
    );

    await waitFor(() => {
      expect(screen.getByText("The App Shell Content")).toBeInTheDocument();
    });

    // replaceState was called with existing window.history.state and stripped hash
    expect(replaceStateSpy).toHaveBeenCalledWith(
      initialHistoryState,
      "",
      "/workspace/app?query=active",
    );
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain("confidential_token");
    expect(window.history.state).toEqual(initialHistoryState);
  });

  it("ignores hash when token is prefixed (e.g. #xtoken=abc)", async () => {
    window.location.hash = "#xtoken=abc";
    let tokenPostCalled = false;
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/token")) {
        tokenPostCalled = true;
        return Promise.resolve(
          new Response(JSON.stringify({ mode: "local", signed_in: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (req.url.includes("/api/auth/status")) {
        return Promise.resolve(
          new Response(JSON.stringify({ mode: "local", signed_in: false }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected request to ${req.url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderWithClient(
      <AuthGate>
        <div>The App Shell Content</div>
      </AuthGate>,
    );

    await waitFor(() => {
      expect(
        screen.getByText("Open the app with the link printed in the terminal where it runs."),
      ).toBeInTheDocument();
    });
    expect(tokenPostCalled).toBe(false);
  });

  it("treats malformed percent-encoding in hash token as a bad link without crashing and preserves history.state", async () => {
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    const initialHistoryState = { page: "profile" };
    window.history.replaceState(initialHistoryState, "", "/account#token=%invalid%");
    replaceStateSpy.mockClear();

    let postedToken: string | undefined = undefined;
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const req = new Request(input, init);
        if (req.url.includes("/api/auth/token") && req.method === "POST") {
          const bodyText = await req.clone().text();
          const parsed = JSON.parse(bodyText || "{}") as Record<string, unknown>;
          postedToken = typeof parsed.token === "string" ? parsed.token : undefined;
          return new Response(
            JSON.stringify({
              code: "bad_token",
              message:
                "This link is out of date. Open the address printed in the terminal where the app runs.",
            }),
            {
              status: 401,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        if (req.url.includes("/api/auth/status")) {
          return Promise.resolve(
            new Response(JSON.stringify({ mode: "local", signed_in: false }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected request to ${req.url}`));
      });
    vi.stubGlobal("fetch", fakeFetch);

    renderWithClient(
      <AuthGate>
        <div>The App Shell Content</div>
      </AuthGate>,
    );

    await waitFor(() => {
      expect(
        screen.getByText(
          "This link is out of date. Open the address printed in the terminal where the app runs.",
        ),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText("Open the app with the link printed in the terminal where it runs."),
    ).toBeInTheDocument();
    expect(postedToken).toBe("%invalid%");
    expect(replaceStateSpy).toHaveBeenCalledWith(initialHistoryState, "", "/account");
    expect(window.history.state).toEqual(initialHistoryState);
    expect(window.location.hash).toBe("");
  });
});
