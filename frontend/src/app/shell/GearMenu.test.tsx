import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GearMenu } from "./GearMenu";
import type { PageRoute } from "../routes";

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
  },
}));

describe("GearMenu", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function renderMenu(appRoutes: PageRoute[] = []) {
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <GearMenu appRoutes={appRoutes} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("surfaces sign-out failure when auth.signOut() rejects", async () => {
    const user = userEvent.setup();

    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/status")) {
        return Promise.resolve(
          new Response(JSON.stringify({ mode: "server", signed_in: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (req.url.includes("/api/auth/logout") && req.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "server_error",
              message: "Failed to log out. Try again.",
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

    renderMenu();

    // Open GearMenu
    await user.click(screen.getByRole("button", { name: /settings/i }));

    // Click Sign out
    const signOutBtn = await screen.findByRole("menuitem", { name: /sign out/i });
    await user.click(signOutBtn);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Failed to log out. Try again.");
    });

    expect(toast.error).toHaveBeenCalledWith("Failed to log out. Try again.");
  });
});
