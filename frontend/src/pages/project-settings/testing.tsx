import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

export interface RecordedRequest {
  path: string;
  method: string;
  body: unknown;
}

/** Answers a stubbed route; the body is the parsed JSON of a PUT or POST, else null. */
export type RouteHandler = (body: unknown) => Response;

export function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function failure(message: string, status = 409): Response {
  return json({ code: "conflict", message, detail: null }, status);
}

/** Stubs `fetch` with handlers keyed "METHOD /path"; an unlisted route answers 404. */
export function stubRoutes(routes: Record<string, RouteHandler>): RecordedRequest[] {
  const requests: RecordedRequest[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      const path = new URL(request.url).pathname;
      const hasBody = request.method === "PUT" || request.method === "POST";
      const text = hasBody ? await request.text() : "";
      const body: unknown = text === "" ? null : JSON.parse(text);
      requests.push({ path, method: request.method, body });
      const handler = routes[`${request.method} ${path}`];
      return handler ? handler(body) : new Response(null, { status: 404 });
    }),
  );
  return requests;
}

export function renderSection(ui: ReactElement): RenderResult {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}
