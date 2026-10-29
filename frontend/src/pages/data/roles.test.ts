import { describe, expect, it } from "vitest";
import type { HandoffView } from "@/lib/useHandoff";
import { modelsUsing, roleFor } from "./roles";

describe("roles", () => {
  const sampleHandoff: HandoffView = {
    proposal_id: "prop_1",
    version: 1,
    approved: "2026-10-29T12:00:00Z",
    payload: {
      commands: [],
      data_roles: [
        {
          connection: "warehouse",
          table: "users",
          role: "training",
          id_column: "user_id",
          time_column: "created_at",
        },
        {
          connection: "datalake",
          table: "users",
          role: "serving_inputs",
          id_column: "uid",
          time_column: null,
        },
        {
          connection: "warehouse",
          table: "predictions_output",
          role: "predictions",
          id_column: null,
          time_column: "predicted_at",
        },
      ],
      edges: [],
      gaps: [],
      grants: {},
      operations: {},
      pipeline: [],
      tracking: [],
      models: [
        {
          name: "churn-model",
          commands: [],
          environments: {},
          production_signal: null,
          profile: {},
          promotion: "person",
          predictions: {
            connection: "warehouse",
            table: "predictions_output",
          },
          labels: {
            connection: "warehouse",
            table: "ground_truth",
          },
        },
        {
          name: "fraud-detector",
          commands: [],
          environments: {},
          production_signal: null,
          profile: {},
          promotion: "team_gate",
          predictions: {
            connection: "warehouse",
            table: "predictions_output",
          },
          labels: null,
        },
        {
          name: "recommendation-model",
          commands: [],
          environments: {},
          production_signal: null,
          profile: {},
          promotion: "person",
          predictions: {
            connection: "datalake",
            table: "predictions_output",
          },
          labels: {
            connection: "warehouse",
            table: "predictions_output",
          },
        },
      ],
    },
  } as unknown as HandoffView;

  describe("roleFor", () => {
    it("returns null when handoff is null", () => {
      expect(roleFor(null, "warehouse", "users")).toBeNull();
    });

    it("matches connection and table", () => {
      const role = roleFor(sampleHandoff, "warehouse", "users");
      expect(role).toEqual({
        connection: "warehouse",
        table: "users",
        role: "training",
        id_column: "user_id",
        time_column: "created_at",
      });
    });

    it("differentiates one table name under two different connections", () => {
      const warehouseRole = roleFor(sampleHandoff, "warehouse", "users");
      const datalakeRole = roleFor(sampleHandoff, "datalake", "users");

      expect(warehouseRole?.role).toBe("training");
      expect(warehouseRole?.id_column).toBe("user_id");

      expect(datalakeRole?.role).toBe("serving_inputs");
      expect(datalakeRole?.id_column).toBe("uid");
    });

    it("returns null when no match is found", () => {
      expect(roleFor(sampleHandoff, "warehouse", "unknown_table")).toBeNull();
      expect(roleFor(sampleHandoff, "other_connection", "users")).toBeNull();
    });
  });

  describe("modelsUsing", () => {
    it("returns empty array when handoff is null", () => {
      expect(modelsUsing(null, "warehouse", "users")).toEqual([]);
    });

    it("returns models using the table as predictions or labels", () => {
      const using = modelsUsing(sampleHandoff, "warehouse", "predictions_output");
      expect(using).toEqual([
        { model: "churn-model", as: "predictions" },
        { model: "fraud-detector", as: "predictions" },
        { model: "recommendation-model", as: "labels" },
      ]);
    });

    it("returns empty array when no models use the table", () => {
      expect(modelsUsing(sampleHandoff, "warehouse", "users")).toEqual([]);
    });
  });
});
