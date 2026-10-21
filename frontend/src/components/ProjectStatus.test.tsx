import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ProjectStatus } from "./ProjectStatus";

describe("ProjectStatus", () => {
  it("renders 'OK' for ok status without loud badge and no detail text", () => {
    render(<ProjectStatus status="ok" />);
    expect(screen.getByText("OK")).toBeInTheDocument();
  });

  it("renders warning detail text for ok status with status_detail (kept the last copy)", () => {
    render(
      <ProjectStatus status="ok" status_detail="Refresh failed; kept the last downloaded copy." />,
    );
    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(screen.getByText("Refresh failed; kept the last downloaded copy.")).toBeInTheDocument();
  });

  it("renders 'Folder not found' and the detail for missing status", () => {
    render(<ProjectStatus status="missing" status_detail="/home/user/missing-repo" />);
    expect(screen.getByText("Folder not found")).toBeInTheDocument();
    expect(screen.getByText("/home/user/missing-repo")).toBeInTheDocument();
  });

  it("renders 'No committed files yet. Commit your files first.' for empty status", () => {
    render(<ProjectStatus status="empty" />);
    expect(
      screen.getByText("No committed files yet. Commit your files first."),
    ).toBeInTheDocument();
  });

  it("renders 'Download failed' and the detail for clone_failed status", () => {
    render(
      <ProjectStatus
        status="clone_failed"
        status_detail="git clone failed: remote repository not found"
      />,
    );
    expect(screen.getByText("Download failed")).toBeInTheDocument();
    expect(screen.getByText("git clone failed: remote repository not found")).toBeInTheDocument();
  });
});
