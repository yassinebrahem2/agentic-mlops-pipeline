import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PageFrame } from "./PageFrame";

describe("PageFrame", () => {
  it("renders children, centers content, and applies default 4xl width and padding", () => {
    render(<PageFrame>Hello page</PageFrame>);
    const frame = screen.getByText("Hello page");
    expect(frame).toBeInTheDocument();
    expect(frame).toHaveClass("mx-auto", "w-full", "p-6", "max-w-4xl");
  });

  it.each([
    ["3xl", "max-w-3xl"],
    ["4xl", "max-w-4xl"],
    ["5xl", "max-w-5xl"],
    ["6xl", "max-w-6xl"],
  ] as const)("applies %s width class", (width, expectedClass) => {
    render(<PageFrame width={width}>Content</PageFrame>);
    expect(screen.getByText("Content")).toHaveClass(expectedClass);
  });

  it("merges custom className", () => {
    render(<PageFrame className="space-y-6 custom-class">Content</PageFrame>);
    const frame = screen.getByText("Content");
    expect(frame).toHaveClass("mx-auto", "w-full", "p-6", "max-w-4xl", "space-y-6", "custom-class");
  });
});
