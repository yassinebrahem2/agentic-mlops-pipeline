import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppInfo } from "./useAppInfo";

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

describe("useAppInfo", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("fetches app info using queryKey ['app']", async () => {
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes("/api/app")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              name: "mlops_agents",
              version: "0.1.0",
              mode: "local",
              data_dir: "/data",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.reject(new Error(`Unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useAppInfo(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toEqual({
      name: "mlops_agents",
      version: "0.1.0",
      mode: "local",
      data_dir: "/data",
    });
  });

  it("surfaces error when request fails", async () => {
    const fakeFetch = vi.fn().mockImplementation(() => {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            code: "internal_error",
            message: "Failed to load app info",
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useAppInfo(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });
});
