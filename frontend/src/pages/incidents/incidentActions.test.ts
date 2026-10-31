import { describe, expect, it } from "vitest";
import { incidentActions } from "./incidentActions";
import { incident } from "./testing";

describe("incidentActions", () => {
  it.each([
    ["open", incident("i", { status: "open" }), { canAcknowledge: true, canClose: true }],
    [
      "acknowledged",
      incident("i", { status: "acknowledged", acknowledged_at: "2026-10-30T10:00:00Z" }),
      { canAcknowledge: false, canClose: true },
    ],
    [
      "resolved",
      incident("i", { status: "resolved", resolved_at: "2026-10-30T10:00:00Z" }),
      { canAcknowledge: false, canClose: true },
    ],
    [
      "closed",
      incident("i", {
        status: "closed",
        resolved_at: "2026-10-30T10:00:00Z",
        closed_at: "2026-10-30T11:00:00Z",
      }),
      { canAcknowledge: false, canClose: false },
    ],
    [
      "merged",
      incident("i", { status: "merged", merged_into: "inc_9" }),
      { canAcknowledge: false, canClose: false },
    ],
  ])("%s", (_name, view, expected) => {
    expect(incidentActions(view)).toEqual(expected);
  });
});
