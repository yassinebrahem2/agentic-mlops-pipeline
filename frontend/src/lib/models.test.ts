import { describe, expect, it } from "vitest";
import type { components } from "@/api/schema";
import { hostedEmbedderWarning, hostedModelWarning, usableModels } from "./models";

type ModelEntry = components["schemas"]["ModelEntry"];

const baseChat: ModelEntry = {
  address: "fake:fake-chat",
  model_type: "chat",
  provider_id: "prv_fake",
  provider_name: "fake",
  wire_model_id: "fake-chat",
  context_window: 4096,
  tool_calling: true,
  streaming: true,
  availability: "verified",
  availability_detail: null,
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const brokenChat: ModelEntry = {
  ...baseChat,
  address: "fake:broken-chat",
  tool_calling: false,
  streaming: false,
  availability: "needs_attention",
  availability_detail: "failed tool call",
  problem: "The model fake:broken-chat failed: failed tool call.",
};

const untestedChat: ModelEntry = {
  ...baseChat,
  address: "fake:untested-chat",
  availability: "untested",
  problem: "The model fake:untested-chat has not passed verification (tool calls and streaming).",
};

const baseEmbedder: ModelEntry = {
  address: "fake:fake-embed",
  model_type: "embedder",
  provider_id: "prv_fake",
  provider_name: "fake",
  wire_model_id: "fake-embed",
  context_window: null,
  dimensions: 1536,
  tool_calling: false,
  streaming: false,
  availability: "verified",
  availability_detail: null,
  verified_at: "2026-10-21T10:00:00+00:00",
  problem: null,
};

const brokenEmbedder: ModelEntry = {
  ...baseEmbedder,
  address: "fake:broken-embed",
  dimensions: null,
  availability: "needs_attention",
  availability_detail: "dimension mismatch",
  problem: "The model fake:broken-embed failed: dimension mismatch.",
};

const untestedEmbedder: ModelEntry = {
  ...baseEmbedder,
  address: "fake:untested-embed",
  dimensions: null,
  availability: "untested",
  problem: "The embedder fake:untested-embed has not passed verification.",
};

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

  describe("hostedEmbedderWarning", () => {
    it("returns the exact warning text for a hosted embedder", () => {
      expect(hostedEmbedderWarning("Groq")).toBe(
        "Note titles and text are sent to Groq to compute their vectors.",
      );
      expect(hostedEmbedderWarning("OpenAI")).toBe(
        "Note titles and text are sent to OpenAI to compute their vectors.",
      );
    });
  });

  describe("usableModels", () => {
    it("returns usable chat models when filtered for chat", () => {
      const all = [
        baseChat,
        brokenChat,
        untestedChat,
        baseEmbedder,
        brokenEmbedder,
        untestedEmbedder,
      ];
      expect(usableModels(all, "chat")).toEqual([baseChat]);
    });

    it("returns usable embedder models when filtered for embedder", () => {
      const all = [
        baseChat,
        brokenChat,
        untestedChat,
        baseEmbedder,
        brokenEmbedder,
        untestedEmbedder,
      ];
      expect(usableModels(all, "embedder")).toEqual([baseEmbedder]);
    });

    it("returns empty array when no models match type and problem null", () => {
      expect(usableModels([brokenChat, untestedChat, baseEmbedder], "chat")).toEqual([]);
      expect(usableModels([brokenEmbedder, untestedEmbedder, baseChat], "embedder")).toEqual([]);
    });
  });
});
