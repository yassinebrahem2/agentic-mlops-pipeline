import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceCredentialForm } from "./SourceCredentialForm";
import { failure, json, renderSection, stubRoutes } from "./testing";

const BASE = "/api/projects/prj_demo";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("SourceCredentialForm", () => {
  it("renders password input and Set button", () => {
    stubRoutes({});
    renderSection(<SourceCredentialForm projectId="prj_demo" sourceId="src_1" />);

    const input = screen.getByLabelText("Credential");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    expect(screen.getByRole("button", { name: "Set" })).toBeInTheDocument();
  });

  it("submits credential, clears input on success", async () => {
    const user = userEvent.setup();
    const requests = stubRoutes({
      [`PUT ${BASE}/tracking/sources/src_1/credential`]: () => json({}),
    });

    renderSection(<SourceCredentialForm projectId="prj_demo" sourceId="src_1" />);

    const input = screen.getByLabelText("Credential");
    await user.type(input, "my-token");
    await user.click(screen.getByRole("button", { name: "Set" }));

    await waitFor(() => {
      expect(input).toHaveValue("");
    });
    expect(requests).toContainEqual({
      path: `${BASE}/tracking/sources/src_1/credential`,
      method: "PUT",
      body: { credential: "my-token" },
    });
  });

  it("shows initial error if passed, and displays server error on failure", async () => {
    const user = userEvent.setup();
    stubRoutes({
      [`PUT ${BASE}/tracking/sources/src_1/credential`]: () =>
        failure("Invalid credential value", 400),
    });

    renderSection(
      <SourceCredentialForm
        projectId="prj_demo"
        sourceId="src_1"
        initialError="Initial auth failure"
      />,
    );

    expect(screen.getByText("Initial auth failure")).toBeInTheDocument();

    const input = screen.getByLabelText("Credential");
    await user.type(input, "new-token");
    await user.click(screen.getByRole("button", { name: "Set" }));

    expect(await screen.findByText("Invalid credential value")).toBeInTheDocument();
    expect(input).toHaveValue("");
  });

  it("updates displayed error when initialError prop changes post-mount", () => {
    stubRoutes({});
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <SourceCredentialForm projectId="prj_demo" sourceId="src_1" initialError={null} />
      </QueryClientProvider>,
    );

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    rerender(
      <QueryClientProvider client={queryClient}>
        <SourceCredentialForm
          projectId="prj_demo"
          sourceId="src_1"
          initialError="Updated post-mount error"
        />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Updated post-mount error")).toBeInTheDocument();
  });

  it("after submit, no entry of queryClient.getMutationCache().getAll() has the credential in state.variables", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    stubRoutes({
      [`PUT ${BASE}/tracking/sources/src_1/credential`]: () => json({}),
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SourceCredentialForm projectId="prj_demo" sourceId="src_1" />
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Credential");
    await user.type(input, "SECRET123");
    await user.click(screen.getByRole("button", { name: "Set" }));

    await waitFor(() => {
      expect(input).toHaveValue("");
    });

    const mutations = queryClient.getMutationCache().getAll();
    for (const m of mutations) {
      const vars = m.state.variables as Record<string, unknown> | undefined;
      expect(vars?.credential).toBeUndefined();
    }
  });

  it("after failing submit, credential is also absent from queryClient.getMutationCache()", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    stubRoutes({
      [`PUT ${BASE}/tracking/sources/src_1/credential`]: () =>
        failure("Invalid credential value", 400),
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SourceCredentialForm projectId="prj_demo" sourceId="src_1" />
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Credential");
    await user.type(input, "SECRET123");
    await user.click(screen.getByRole("button", { name: "Set" }));

    expect(await screen.findByText("Invalid credential value")).toBeInTheDocument();

    const mutations = queryClient.getMutationCache().getAll();
    for (const m of mutations) {
      const vars = m.state.variables as Record<string, unknown> | undefined;
      expect(vars?.credential).toBeUndefined();
    }
  });
});
