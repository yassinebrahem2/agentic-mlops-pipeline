import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RunStatus } from "@/lib/useTracking";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  const cases: Array<{ status: RunStatus; expectedLabel: string }> = [
    { status: "running", expectedLabel: "Running" },
    { status: "succeeded", expectedLabel: "Succeeded" },
    { status: "failed", expectedLabel: "Failed" },
    { status: "cancelled", expectedLabel: "Cancelled" },
    { status: "other", expectedLabel: "Other" },
  ];

  cases.forEach(({ status, expectedLabel }) => {
    it(`renders label and styling for status "${status}"`, () => {
      render(<StatusBadge status={status} />);
      const badge = screen.getByText(expectedLabel);
      expect(badge).toBeInTheDocument();
      expect(badge.getAttribute("data-slot")).toBe("badge");
    });
  });

  it("destructive variant is applied for failed status", () => {
    render(<StatusBadge status="failed" />);
    const badge = screen.getByText("Failed");
    expect(badge.getAttribute("data-variant")).toBe("destructive");
  });
});
