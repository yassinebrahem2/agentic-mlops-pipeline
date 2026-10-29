import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import * as useProjectModule from "@/lib/useProject";
import { AgentsPage } from "./AgentsPage";
import { FakeStream } from "./session/testing";
import * as useSessionModule from "./session/useSession";

describe("AgentsPage states", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders nothing and calls no project route or hook while no project is known", async () => {
    const getSpy = vi.spyOn(api, "GET");
    const postSpy = vi.spyOn(api, "POST");
    const openStreamSpy = vi
      .spyOn(streamModule, "openStream")
      .mockImplementation((url: string) => new FakeStream(url));
    const useProjectSpy = vi.spyOn(useProjectModule, "useProject");
    const useSessionSpy = vi.spyOn(useSessionModule, "useSession");

    const router = createMemoryRouter([{ path: "/agents", element: <AgentsPage /> }], {
      initialEntries: ["/agents"],
    });

    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    await act(() => Promise.resolve());

    expect(container).toBeEmptyDOMElement();
    expect(useProjectSpy).not.toHaveBeenCalled();
    expect(useSessionSpy).not.toHaveBeenCalled();
    expect(getSpy).not.toHaveBeenCalled();
    expect(postSpy).not.toHaveBeenCalled();
    expect(openStreamSpy).not.toHaveBeenCalled();
  });
});
