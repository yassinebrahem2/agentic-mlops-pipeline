import { describe, expect, it } from "vitest";
import { fromSelectValue, SELECT_NONE, toSelectValue } from "./selectNone";

describe("selectNone", () => {
  it("defines a non-empty sentinel string", () => {
    expect(typeof SELECT_NONE).toBe("string");
    expect(SELECT_NONE.length).toBeGreaterThan(0);
  });

  describe("toSelectValue", () => {
    it("maps null and undefined to SELECT_NONE", () => {
      expect(toSelectValue(null)).toBe(SELECT_NONE);
      expect(toSelectValue(undefined)).toBe(SELECT_NONE);
    });

    it("preserves non-empty string values", () => {
      expect(toSelectValue("groq:llama")).toBe("groq:llama");
      expect(toSelectValue("custom-model")).toBe("custom-model");
    });
  });

  describe("fromSelectValue", () => {
    it("maps SELECT_NONE to null", () => {
      expect(fromSelectValue(SELECT_NONE)).toBeNull();
    });

    it("preserves non-sentinel string values", () => {
      expect(fromSelectValue("groq:llama")).toBe("groq:llama");
      expect(fromSelectValue("custom-model")).toBe("custom-model");
    });
  });
});
