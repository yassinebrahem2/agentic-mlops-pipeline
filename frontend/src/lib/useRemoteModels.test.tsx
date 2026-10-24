import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createQueryWrapper } from "../test/queryWrapper";
import { useRemoteModels } from "./useRemoteModels";

describe("useRemoteModels", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("fetches remote models using queryKey ['remote-models', provider]", async () => {
    const fakeModels = [
      { id: "model-1", context_window: 8192 },
      { id: "model-2", context_window: null },
    ];
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes("/api/providers/gemini/remote-models")) {
        return Promise.resolve(
          new Response(JSON.stringify(fakeModels), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.reject(new Error(`Unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useRemoteModels("gemini"), {
      wrapper: createQueryWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toEqual(fakeModels);
  });

  it("does not fetch when enabled is false", () => {
    const fakeFetch = vi.fn();
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useRemoteModels("gemini", { enabled: false }), {
      wrapper: createQueryWrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(fakeFetch).not.toHaveBeenCalled();
  });

  it("does not fetch when provider is empty", () => {
    const fakeFetch = vi.fn();
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useRemoteModels(""), {
      wrapper: createQueryWrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(fakeFetch).not.toHaveBeenCalled();
  });

  it("surfaces error when GET fails", async () => {
    const fakeFetch = vi.fn().mockImplementation(() => {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            code: "provider_error",
            message: "Failed to list models",
          }),
          { status: 502, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useRemoteModels("gemini"), {
      wrapper: createQueryWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });
});
