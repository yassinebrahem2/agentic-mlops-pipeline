import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AddTrackingSourceDialog } from "./AddTrackingSourceDialog";
import { renderSection, stubRoutes } from "./testing";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("AddTrackingSourceDialog", () => {
  it("renders dialog fields when open", () => {
    stubRoutes({});
    renderSection(
      <AddTrackingSourceDialog projectId="prj_demo" open={true} onOpenChange={vi.fn()} />,
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Type" })).toBeInTheDocument();
    expect(screen.getByLabelText("URL")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Needs a credential" })).toBeInTheDocument();
  });

  it("switches to GitHub Actions with repository and workflows", async () => {
    const user = userEvent.setup();
    stubRoutes({});
    renderSection(
      <AddTrackingSourceDialog projectId="prj_demo" open={true} onOpenChange={vi.fn()} />,
    );

    const typeSelect = screen.getByRole("combobox", { name: "Type" });
    await user.click(typeSelect);
    await user.click(await screen.findByRole("option", { name: "GitHub Actions" }));

    expect(screen.getByLabelText("Repository")).toBeInTheDocument();
    expect(screen.getByLabelText("Workflows")).toBeInTheDocument();
    expect(screen.queryByLabelText("URL")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Needs a credential" })).not.toBeInTheDocument();
  });
});
