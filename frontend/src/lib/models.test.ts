import { describe, expect, it } from "vitest";
import { hostedModelWarning } from "./models";

describe("models", () => {
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
