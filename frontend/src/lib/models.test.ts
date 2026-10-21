import { describe, expect, it } from "vitest";
import { hostedModelWarning, isUsableModel } from "./models";

describe("models", () => {
  describe("isUsableModel", () => {
    it("returns true only when model is verified, has tool_calling, streaming, and positive context_window", () => {
      const valid = {
        availability: "verified",
        tool_calling: true,
        streaming: true,
        context_window: 8192,
      };
      expect(isUsableModel(valid)).toBe(true);

      expect(isUsableModel({ ...valid, availability: "unverified" })).toBe(false);
      expect(isUsableModel({ ...valid, tool_calling: false })).toBe(false);
      expect(isUsableModel({ ...valid, streaming: false })).toBe(false);
      expect(isUsableModel({ ...valid, context_window: null })).toBe(false);
      expect(isUsableModel({ ...valid, context_window: 0 })).toBe(false);
      expect(isUsableModel({ ...valid, context_window: -1 })).toBe(false);
    });
  });

  describe("hostedModelWarning", () => {
    it("returns the exact warning text for a hosted provider", () => {
      expect(hostedModelWarning("Groq")).toBe(
        "Files the agent reads are sent to Groq. A secret inside a committed file whose name does not look secret can reach it.",
      );
      expect(hostedModelWarning("Gemini")).toBe(
        "Files the agent reads are sent to Gemini. A secret inside a committed file whose name does not look secret can reach it.",
      );
    });
  });
});
