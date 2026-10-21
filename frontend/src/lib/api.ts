import createClient, { type Client } from "openapi-fetch";

export type paths = import("../api/schema").paths;

// Node/undici in tests throws ERR_INVALID_URL on relative URLs without a base origin.
// In browser environments, new Request('/') resolves to the current origin.
try {
  new Request("/");
} catch {
  const OriginalRequest = globalThis.Request;
  globalThis.Request = class extends OriginalRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      if (typeof input === "string" && input.startsWith("/")) {
        const origin =
          typeof window !== "undefined" && window.location.origin
            ? window.location.origin
            : "http://localhost";
        input = `${origin}${input}`;
      }
      super(input, init);
    }
  };
}

export class ApiError extends Error {
  code: string;
  status: number;
  detail: unknown;

  constructor(code: string, message: string, status?: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status ?? 500;
    this.detail = detail;
  }
}

function getCsrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return match && match[1] ? decodeURIComponent(match[1]) : undefined;
}

export function createApiClient<P extends object>(fetchImpl?: typeof fetch): Client<P> {
  const client = createClient<P>({
    baseUrl: "",
    fetch:
      fetchImpl ??
      ((input: RequestInfo | URL, init?: RequestInit) => globalThis.fetch(input, init)),
  });

  client.use({
    onRequest({ request }) {
      if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) {
        const csrf = getCsrfToken();
        if (csrf) {
          request.headers.set("X-CSRF-Token", csrf);
        }
      }
      return request;
    },
    onError({ error }) {
      if (error instanceof ApiError) {
        return error;
      }
      return new ApiError(
        "offline",
        "The app's server is not reachable. Check that it is still running.",
        0,
      );
    },
  });

  return client;
}

export const api: Client<paths> = createApiClient<paths>();

export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined) {
    return result.data;
  }

  const status = result.response.status;
  if (status === 0) {
    throw new ApiError(
      "offline",
      "The app's server is not reachable. Check that it is still running.",
      0,
    );
  }

  const err = result.error;
  if (
    err !== null &&
    typeof err === "object" &&
    "code" in err &&
    typeof err.code === "string" &&
    "message" in err &&
    typeof err.message === "string"
  ) {
    const errorObj = err as { code: string; message: string; detail?: unknown };
    throw new ApiError(errorObj.code, errorObj.message, status, errorObj.detail);
  }

  throw new ApiError(
    `http_${String(status)}`,
    `The server answered with an error (HTTP ${String(status)}).`,
    status,
    err,
  );
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return "Something went wrong. Try again.";
}

export function fieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError) || error.code !== "invalid_request") {
    return {};
  }

  const detail = error.detail;
  if (!detail || typeof detail !== "object" || !("errors" in detail)) {
    return {};
  }

  const errors = (detail as Record<string, unknown>).errors;
  if (!Array.isArray(errors)) {
    return {};
  }

  const result: Record<string, string> = {};
  for (const item of errors as unknown[]) {
    if (item !== null && typeof item === "object" && "field" in item && "message" in item) {
      const field = (item as Record<string, unknown>).field;
      const message = (item as Record<string, unknown>).message;
      if (typeof field === "string" && typeof message === "string") {
        const fieldName = field.replace(/^body\./, "");
        result[fieldName] = message;
      }
    }
  }

  return result;
}
