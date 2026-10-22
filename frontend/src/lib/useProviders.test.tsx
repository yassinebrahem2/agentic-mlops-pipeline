import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProviders } from "./useProviders";

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("useProviders", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("fetches providers using queryKey ['providers']", async () => {
    const fakeProviders = [
      {
        id: "prv_1",
        name: "provider1",
        kind: "hosted",
        base_url: "https://api.example.com",
        status: "ok",
        status_detail: null,
        keys: [],
      },
    ];
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes("/api/providers")) {
        return Promise.resolve(
          new Response(JSON.stringify(fakeProviders), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProviders(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toEqual(fakeProviders);
  });

  it("surfaces error when GET /api/providers fails", async () => {
    const fakeFetch = vi.fn().mockImplementation(() => {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            code: "internal_error",
            message: "Failed to load providers",
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useProviders(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });
});
