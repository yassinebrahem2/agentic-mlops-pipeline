import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGate } from "./AuthGate";
import { resetTokenExchange } from "./useAuth";
import { GearMenu } from "../shell/GearMenu";

describe("AuthGate", () => {
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
    window.location.hash = "";
    history.replaceState(null, "", "/");
    document.cookie = "csrf_token=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  });

  function renderWithClient(ui: React.ReactElement) {
    return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  }

  it("1. #token=abc posts token exactly once in StrictMode, removes hash, and shows shell", async () => {
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

  it("2. rejected token shows bad_token message and local-mode instructions", async () => {
    window.location.hash = "#token=expired_token";

    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/token") && req.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "bad_token",
              message: "The access link is invalid or has expired.",
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
      expect(screen.getByText("The access link is invalid or has expired.")).toBeInTheDocument();
    });

    expect(
      screen.getByText("Open the app with the link printed in the terminal where it runs."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The App Shell Content")).not.toBeInTheDocument();
  });

  it("3. server mode shows password form, wrong password shows error, right password shows shell", async () => {
    const user = userEvent.setup();

    let signedIn = false;
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const req = new Request(input, init);
        if (req.url.includes("/api/auth/status")) {
          return new Response(
            JSON.stringify({
              mode: "server",
              signed_in: signedIn,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        if (req.url.includes("/api/auth/login") && req.method === "POST") {
          const bodyText = await req.clone().text();
          const body = JSON.parse(bodyText || "{}") as { password?: string };
          if (body.password === "secret123") {
            signedIn = true;
            return new Response(
              JSON.stringify({
                mode: "server",
                signed_in: true,
              }),
              {
                status: 200,
                headers: { "Content-Type": "application/json" },
              },
            );
          } else {
            return new Response(
              JSON.stringify({
                code: "bad_password",
                message: "Wrong password.",
              }),
              {
                status: 401,
                headers: { "Content-Type": "application/json" },
              },
            );
          }
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
      expect(screen.getByLabelText("Password")).toBeInTheDocument();
    });
    expect(screen.queryByText("The App Shell Content")).not.toBeInTheDocument();

    // Wrong password
    await user.type(screen.getByLabelText("Password"), "wrongpass");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByText("Wrong password.")).toBeInTheDocument();
    });
    expect(screen.queryByText("The App Shell Content")).not.toBeInTheDocument();

    // Correct password
    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), "secret123");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByText("The App Shell Content")).toBeInTheDocument();
    });
  });

  it("4. Sign out posts logout and returns to the sign-in screen", async () => {
    const user = userEvent.setup();
    document.cookie = "csrf_token=test-csrf-token; path=/";

    let signedIn = true;
    let logoutCsrfHeader: string | null = null;
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/status")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              mode: "server",
              signed_in: signedIn,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      if (req.url.includes("/api/auth/logout") && req.method === "POST") {
        logoutCsrfHeader = req.headers.get("X-CSRF-Token");
        signedIn = false;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              mode: "server",
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
        <div>
          <div>The App Shell Content</div>
          <GearMenu appRoutes={[]} />
        </div>
      </AuthGate>,
    );

    await waitFor(() => {
      expect(screen.getByText("The App Shell Content")).toBeInTheDocument();
    });

    // Open GearMenu
    await user.click(screen.getByRole("button", { name: /settings/i }));

    // Click Sign out
    const signOutBtn = await screen.findByRole("menuitem", { name: /sign out/i });
    await user.click(signOutBtn);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    });
    expect(screen.queryByText("The App Shell Content")).not.toBeInTheDocument();
    expect(logoutCsrfHeader).toBe("test-csrf-token");
  });

  it("5. token never appears in history after exchange (check href and hash)", async () => {
    window.location.hash = "#token=confidential_token";
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

    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain("confidential_token");
  });

  it("renders quiet loading state while status loads without flashing sign-in page", () => {
    // Keep status pending
    const fakeFetch = vi.fn().mockReturnValue(new Promise(() => {}));
    vi.stubGlobal("fetch", fakeFetch);

    renderWithClient(
      <AuthGate>
        <div>The App Shell Content</div>
      </AuthGate>,
    );

    expect(screen.queryByText("The App Shell Content")).not.toBeInTheDocument();
    expect(screen.queryByText("Sign in")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Open the app with the link printed in the terminal where it runs."),
    ).not.toBeInTheDocument();
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

  it("treats malformed percent-encoding in hash token as a bad link without crashing", async () => {
    window.location.hash = "#token=%invalid%";
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
      expect(screen.getByText("The access link is invalid or has expired.")).toBeInTheDocument();
    });
    expect(
      screen.getByText("Open the app with the link printed in the terminal where it runs."),
    ).toBeInTheDocument();
    expect(tokenPostCalled).toBe(false);
    expect(window.location.hash).toBe("");
  });

  it("displays errorText when status request fails", async () => {
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/status")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "internal_error",
              message: "Something went wrong on the server.",
            }),
            {
              status: 500,
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
      expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong on the server.");
    });
    expect(screen.queryByText("The App Shell Content")).not.toBeInTheDocument();
    expect(screen.queryByText("Sign in")).not.toBeInTheDocument();
  });

  it("preserves shell content and does not show error screen on background refetch failure when signed in", async () => {
    let callCount = 0;
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/status")) {
        callCount++;
        if (callCount === 1) {
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
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "internal_error",
              message: "Network glitch on background refresh",
            }),
            {
              status: 500,
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

    // Trigger a refetch that fails
    try {
      await queryClient.refetchQueries({ queryKey: ["auth"] });
    } catch {
      // Expected: refetch rejects on 500 error
    }

    // Shell content should still be rendered; error screen should NOT be shown
    await waitFor(() => {
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
    expect(screen.getByText("The App Shell Content")).toBeInTheDocument();
  });
});
