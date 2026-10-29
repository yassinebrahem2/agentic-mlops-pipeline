import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import * as useProjectModule from "@/lib/useProject";
import { FakeStream } from "@/pages/agents/session/testing";
import { ProjectSettingsPage } from "./ProjectSettingsPage";

describe("ProjectSettingsPage states", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders nothing and calls no project route or hook while no project is known", async () => {
    const getSpy = vi.spyOn(api, "GET");
    const openStreamSpy = vi
      .spyOn(streamModule, "openStream")
      .mockImplementation((url: string) => new FakeStream(url));
    const useProjectSpy = vi.spyOn(useProjectModule, "useProject");

    const router = createMemoryRouter([{ path: "/settings", element: <ProjectSettingsPage /> }], {
      initialEntries: ["/settings"],
    });

    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    await act(() => Promise.resolve());

    expect(container).toBeEmptyDOMElement();
    expect(useProjectSpy).not.toHaveBeenCalled();
    expect(getSpy).not.toHaveBeenCalled();
    expect(openStreamSpy).not.toHaveBeenCalled();
  });
});
