import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth, type AuthStatus } from "./useAuth";

describe("useAuth", () => {
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
    document.cookie = "csrf_token=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  });

  function wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client: queryClient }, children);
  }

  it("loads auth status from GET /api/auth/status", async () => {
    const mockStatus: AuthStatus = { mode: "local", signed_in: true };
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/status")) {
        return Promise.resolve(
          new Response(JSON.stringify(mockStatus), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected request to ${req.url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => {
      expect(result.current.status).toEqual(mockStatus);
    });
  });

  it("signOut calls POST /api/auth/logout, updates status, and clears query cache", async () => {
    document.cookie = "csrf_token=test-csrf-token; path=/";
    const signedInStatus: AuthStatus = { mode: "server", signed_in: true };
    const signedOutStatus: AuthStatus = { mode: "server", signed_in: false };

    queryClient.setQueryData(["auth"], signedInStatus);
    queryClient.setQueryData(["some-other-data"], { data: 123 });

    let csrfHeaderSeen: string | null = null;
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/logout") && req.method === "POST") {
        csrfHeaderSeen = req.headers.get("X-CSRF-Token");
        return Promise.resolve(
          new Response(JSON.stringify(signedOutStatus), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected request to ${req.url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useAuth(), { wrapper });
    expect(result.current.status).toEqual(signedInStatus);

    await result.current.signOut();

    expect(fakeFetch).toHaveBeenCalled();
    expect(csrfHeaderSeen).toBe("test-csrf-token");

    await waitFor(() => {
      expect(result.current.status).toEqual(signedOutStatus);
    });
    expect(queryClient.getQueryData(["some-other-data"])).toBeUndefined();
  });
});
