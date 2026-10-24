/// <reference types="node" />

import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const indexCssPath = resolve(__dirname, "index.css");
const uiComponentsDir = resolve(__dirname, "components", "ui");

describe("Global Styles (index.css)", () => {
  const css = readFileSync(indexCssPath, "utf-8");

  it("sets pointer cursor on interactive elements when enabled", () => {
    const requiredSelectors = [
      "button:not(:disabled)",
      '[role="button"]:not([aria-disabled="true"])',
      "a[href]",
      "summary",
      "label[for]",
      "select:not(:disabled)",
      'input[type="checkbox"]:not(:disabled)',
      'input[type="radio"]:not(:disabled)',
    ];

    for (const selector of requiredSelectors) {
      expect(css).toContain(selector);
    }
    expect(css).toMatch(/(?:button:not\(:disabled\)[\s\S]*?)\{\s*cursor:\s*pointer;\s*\}/);
  });

  it("defines base-layer focus-visible outline with --ring and outline-offset", () => {
    expect(css).toMatch(
      /:focus-visible\s*\{\s*outline:\s*2px\s+solid\s+var\(--ring\);\s*outline-offset:\s*2px;\s*\}/,
    );
  });

  it("sets accent-color on checkboxes and radios", () => {
    expect(css).toMatch(
      /input\[type="checkbox"\][\s\S]*?input\[type="radio"\][\s\S]*?\{\s*accent-color:\s*var\(--primary\);\s*\}/,
    );
  });

  it("does not set outline-ring/50 on wildcard selector", () => {
    expect(css).not.toContain("outline-ring/50");
  });
});

describe("UI Components Focus Styling", () => {
  it("does not contain per-component focus-visible:ring in components/ui", () => {
    const files = readdirSync(uiComponentsDir).filter((file: string) => file.endsWith(".tsx"));
    expect(files.length).toBeGreaterThan(0);

    const violatingFiles: string[] = [];
    for (const file of files) {
      const content = readFileSync(resolve(uiComponentsDir, file), "utf-8");
      if (content.includes("focus-visible:ring")) {
        violatingFiles.push(file);
      }
    }

    expect(violatingFiles).toEqual([]);
  });
});
