import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";
import * as streamModule from "@/lib/stream";
import { FakeStream } from "@/pages/agents/session/testing";

export interface RecordedRequest {
  path: string;
  method: string;
  body: unknown;
}

/** Answers a stubbed route; the body is the parsed JSON of a PUT, POST, or PATCH, else null. */
export type RouteHandler = (body: unknown) => Response | Promise<Response>;

export function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function failure(message: string, status = 409): Response {
  return json({ code: "conflict", message, detail: null }, status);
}

/** One property of a parsed request body, or undefined when the body is not an object with it. */
export function bodyField(body: unknown, key: string): unknown {
  if (body === null || typeof body !== "object") return undefined;
  const entry = Object.entries(body).find(([name]) => name === key);
  const value: unknown = entry?.[1];
  return value;
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
      const hasBody =
        request.method === "PUT" || request.method === "POST" || request.method === "PATCH";
      const text = hasBody ? await request.text() : "";
      const body: unknown = text === "" ? null : JSON.parse(text);
      requests.push({ path, method: request.method, body });
      const handler = routes[`${request.method} ${path}`];
      return handler ? await handler(body) : new Response(null, { status: 404 });
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

/** Replaces the project event stream with a fake one; restore it with `vi.restoreAllMocks()`. */
export function stubEventStream(): FakeStream {
  const stream = new FakeStream("/events");
  vi.spyOn(streamModule, "openStream").mockImplementation(() => stream);
  return stream;
}
