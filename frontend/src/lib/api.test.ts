import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { api, ApiError, createApiClient, errorText, fieldErrors, unwrap } from "./api";
import { queryClient } from "./query";
import { renderPage } from "../test/render";

interface TestPaths {
  "/test": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": { ok: boolean };
          };
        };
      };
    };
    post: {
      responses: {
        200: {
          content: {
            "application/json": { ok: boolean };
          };
        };
      };
    };
    put: {
      responses: {
        200: {
          content: {
            "application/json": { ok: boolean };
          };
        };
      };
    };
    delete: {
      responses: {
        200: {
          content: {
            "application/json": { ok: boolean };
          };
        };
      };
    };
  };
}

describe("api client and utilities", () => {
  beforeEach(() => {
    document.cookie = "csrf_token=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  describe("createApiClient CSRF middleware", () => {
    it("attaches X-CSRF-Token on POST, PUT, DELETE but not on GET", async () => {
      document.cookie = "csrf_token=test-csrf-token-123; path=/";

      const capturedHeaders: Record<string, string | null> = {};

      const fakeFetch: typeof fetch = (input, init) => {
        const req = new Request(input, init);
        capturedHeaders[req.method] = req.headers.get("X-CSRF-Token");
        return Promise.resolve(
          new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      };

      const client = createApiClient<TestPaths>(fakeFetch);

      await client.GET("/test");
      expect(capturedHeaders["GET"]).toBeNull();

      await client.POST("/test");
      expect(capturedHeaders["POST"]).toBe("test-csrf-token-123");

      await client.PUT("/test");
      expect(capturedHeaders["PUT"]).toBe("test-csrf-token-123");

      await client.DELETE("/test");
      expect(capturedHeaders["DELETE"]).toBe("test-csrf-token-123");
    });

    it("does not attach X-CSRF-Token on POST when csrf_token cookie is absent", async () => {
      document.cookie = "other_cookie=value";

      let headerValue: string | null = null;
      const fakeFetch: typeof fetch = (input, init) => {
        const req = new Request(input, init);
        headerValue = req.headers.get("X-CSRF-Token");
        return Promise.resolve(
          new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      };

      const client = createApiClient<TestPaths>(fakeFetch);
      await client.POST("/test");
      expect(headerValue).toBeNull();
    });
  });

  describe("unwrap", () => {
    it("returns data when data is present", () => {
      const response = new Response(JSON.stringify({ ok: true }), { status: 200 });
      const result = unwrap<{ ok: boolean }>({ data: { ok: true }, response });
      expect(result).toEqual({ ok: true });
    });

    it("throws ApiError with server code and message when error shape matches", () => {
      const response = new Response(null, { status: 400 });
      expect(() =>
        unwrap({
          error: { code: "bad_request", message: "Invalid parameters", detail: { extra: 1 } },
          response,
        }),
      ).toThrow(ApiError);

      try {
        unwrap({
          error: { code: "bad_request", message: "Invalid parameters", detail: { extra: 1 } },
          response,
        });
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.code).toBe("bad_request");
        expect(err.message).toBe("Invalid parameters");
        expect(err.status).toBe(400);
        expect(err.detail).toEqual({ extra: 1 });
      }
    });

    it("throws ApiError with http_status code and generic message for non-JSON 502", () => {
      const response = new Response("Bad Gateway", { status: 502 });
      try {
        unwrap({ error: "Bad Gateway", response });
        expect.fail("Expected unwrap to throw");
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.code).toBe("http_502");
        expect(err.message).toBe("The server answered with an error (HTTP 502).");
        expect(err.status).toBe(502);
      }
    });

    it("throws offline ApiError when status is 0 or fetch rejects", async () => {
      const response = { status: 0 } as Response;
      try {
        unwrap({ response });
        expect.fail("Expected unwrap to throw");
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.code).toBe("offline");
        expect(err.message).toBe(
          "The app's server is not reachable. Check that it is still running.",
        );
        expect(err.status).toBe(0);
      }

      // Also verify rejecting client call / promise handling
      const rejectingFetch: typeof fetch = () => Promise.reject(new TypeError("Failed to fetch"));
      const client = createApiClient<TestPaths>(rejectingFetch);
      await expect(client.GET("/test")).rejects.toMatchObject({
        code: "offline",
        message: "The app's server is not reachable. Check that it is still running.",
      });
    });
  });

  describe("errorText", () => {
    it("returns ApiError.message for ApiError instances", () => {
      const err = new ApiError("not_found", "Project not found", 404);
      expect(errorText(err)).toBe("Project not found");
    });

    it("returns generic fallback message for other errors", () => {
      expect(errorText(new Error("Raw server stack trace"))).toBe(
        "Something went wrong. Try again.",
      );
      expect(errorText("plain string error")).toBe("Something went wrong. Try again.");
      expect(errorText(null)).toBe("Something went wrong. Try again.");
      expect(errorText(undefined)).toBe("Something went wrong. Try again.");
    });
  });

  describe("fieldErrors", () => {
    it("maps body.field to field for invalid_request ApiError", () => {
      const err = new ApiError("invalid_request", "Validation failed", 422, {
        errors: [
          { field: "body.base_url", message: "URL is invalid" },
          { field: "body.name", message: "Name is required" },
          { field: "header.x_custom", message: "Header missing" },
        ],
      });
      expect(fieldErrors(err)).toEqual({
        base_url: "URL is invalid",
        name: "Name is required",
        "header.x_custom": "Header missing",
      });
    });

    it("returns empty object for non-invalid_request errors or missing detail", () => {
      const err1 = new ApiError("other_code", "Other error", 400, {
        errors: [{ field: "body.base_url", message: "URL is invalid" }],
      });
      expect(fieldErrors(err1)).toEqual({});

      const err2 = new ApiError("invalid_request", "Validation failed", 422, null);
      expect(fieldErrors(err2)).toEqual({});

      expect(fieldErrors(new Error("Generic"))).toEqual({});
    });
  });

  describe("queryClient 401 handling", () => {
    it("invalidates ['auth'] queries when a query fails with 401", async () => {
      const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

      try {
        await queryClient.query({
          queryKey: ["test-401-query"],
          queryFn: () => {
            throw new ApiError("unauthorized", "Unauthorized", 401);
          },
          retry: false,
        });
      } catch {
        // expected
      }

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["auth"] });
    });

    it("invalidates ['auth'] queries when a mutation fails with 401", async () => {
      const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

      const mutation = queryClient.getMutationCache().build(queryClient, {
        mutationFn: () => {
          throw new ApiError("not_signed_in", "Not signed in", 401);
        },
      });

      try {
        await mutation.execute(undefined);
      } catch {
        // expected
      }

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["auth"] });
    });

    it("does not invalidate ['auth'] on 500 error", async () => {
      const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

      try {
        await queryClient.query({
          queryKey: ["test-500-query"],
          queryFn: () => {
            throw new ApiError("server_error", "Server Error", 500);
          },
          retry: false,
        });
      } catch {
        // expected
      }

      expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: ["auth"] });
    });

    it("retries queries once except on 4xx ApiError", () => {
      const defaultRetry = queryClient.getDefaultOptions().queries?.retry;
      expect(typeof defaultRetry).toBe("function");
      if (typeof defaultRetry === "function") {
        expect(defaultRetry(0, new ApiError("bad_request", "Bad", 400))).toBe(false);
        expect(defaultRetry(0, new ApiError("not_found", "Not found", 404))).toBe(false);
        expect(defaultRetry(0, new ApiError("server_error", "Server", 500))).toBe(true);
        expect(defaultRetry(1, new ApiError("server_error", "Server", 500))).toBe(false);
        expect(defaultRetry(0, new Error("Generic error"))).toBe(true);
        expect(defaultRetry(1, new Error("Generic error"))).toBe(false);
      }
    });
  });

  describe("renderPage test helper", () => {
    it("renders page with custom routes inside fresh QueryClient and memory router", () => {
      const routes = [
        {
          path: "/x",
          element: createElement("div", null, "Rendered X Page"),
        },
      ];

      renderPage("/x", { routes });
      expect(screen.getByText("Rendered X Page")).toBeInTheDocument();
    });

    it("shared api reaches vi.stubGlobal('fetch')", async () => {
      const fakeFetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
      vi.stubGlobal("fetch", fakeFetch);

      await api.GET("/test");
      expect(fakeFetch).toHaveBeenCalled();
    });
  });
});
