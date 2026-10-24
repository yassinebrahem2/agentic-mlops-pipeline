import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { vi } from "vitest";
import { FolderBrowser } from "./FolderBrowser";

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function renderFolderBrowser(props: {
  initialPath?: string;
  onChoose?: (path: string) => void;
  isSubmitting?: boolean;
  busyLabel?: string;
}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <FolderBrowser
        initialPath={props.initialPath}
        onChoose={props.onChoose ?? vi.fn()}
        isSubmitting={props.isSubmitting}
        busyLabel={props.busyLabel}
      />
    </QueryClientProvider>,
  );
}

export function getUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}
