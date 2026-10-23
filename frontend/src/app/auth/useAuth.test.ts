import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { exchangeTokenOnce, useAuth, type AuthStatus } from "./useAuth";

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

  it("signIn calls POST /api/auth/login, updates status, and returns the status", async () => {
    const signedOutStatus: AuthStatus = { mode: "server", signed_in: false };
    const signedInStatus: AuthStatus = { mode: "server", signed_in: true };

    queryClient.setQueryData(["auth"], signedOutStatus);

    let requestBody: unknown = null;
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const req = new Request(input, init);
        if (req.url.includes("/api/auth/login") && req.method === "POST") {
          requestBody = await req.json();
          return new Response(JSON.stringify(signedInStatus), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (req.url.includes("/api/auth/status")) {
          return new Response(JSON.stringify(signedInStatus), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Promise.reject(new Error(`Unexpected request to ${req.url}`));
      });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useAuth(), { wrapper });
    expect(result.current.status).toEqual(signedOutStatus);

    const res = await result.current.signIn("secret-pass");

    expect(requestBody).toEqual({ password: "secret-pass" });
    expect(res).toEqual(signedInStatus);
    expect(queryClient.getQueryData(["auth"])).toEqual(signedInStatus);
  });

  it("signIn throws when POST /api/auth/login fails", async () => {
    const signedOutStatus: AuthStatus = { mode: "server", signed_in: false };
    queryClient.setQueryData(["auth"], signedOutStatus);

    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/login") && req.method === "POST") {
        return Promise.resolve(
          new Response(JSON.stringify({ code: "bad_password", message: "Wrong password." }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected request to ${req.url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useAuth(), { wrapper });

    await expect(result.current.signIn("bad-pass")).rejects.toThrow();
  });

  it("exchangeTokenOnce deduplicates concurrent calls and clears promise on settle", async () => {
    let fetchCount = 0;
    const fakeFetch = vi
      .fn()
      .mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const req = new Request(input, init);
        if (req.url.includes("/api/auth/token") && req.method === "POST") {
          fetchCount++;
          return new Response(JSON.stringify({ mode: "local", signed_in: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Promise.reject(new Error(`Unexpected request to ${req.url}`));
      });
    vi.stubGlobal("fetch", fakeFetch);

    // Concurrent calls share the same promise
    const [res1, res2] = await Promise.all([
      exchangeTokenOnce("tok-abc"),
      exchangeTokenOnce("tok-abc"),
    ]);

    expect(fetchCount).toBe(1);
    expect(res1).toEqual({ status: { mode: "local", signed_in: true } });
    expect(res2).toEqual({ status: { mode: "local", signed_in: true } });

    // After settling, a subsequent call initiates a fresh exchange
    const res3 = await exchangeTokenOnce("tok-abc");
    expect(fetchCount).toBe(2);
    expect(res3).toEqual({ status: { mode: "local", signed_in: true } });
  });

  it("exchangeTokenOnce clears promise on error as well", async () => {
    let fetchCount = 0;
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input, init);
      if (req.url.includes("/api/auth/token") && req.method === "POST") {
        fetchCount++;
        return Promise.resolve(
          new Response(JSON.stringify({ code: "invalid_token", message: "Token expired." }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected request to ${req.url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const res1 = await exchangeTokenOnce("tok-fail");
    expect(fetchCount).toBe(1);
    expect("error" in res1).toBe(true);

    // After settling (with error), next call can exchange again
    const res2 = await exchangeTokenOnce("tok-fail");
    expect(fetchCount).toBe(2);
    expect("error" in res2).toBe(true);
  });
});
