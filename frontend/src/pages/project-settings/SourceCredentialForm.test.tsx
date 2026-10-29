import { screen, waitFor } from "@testing-library/react";
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
});
