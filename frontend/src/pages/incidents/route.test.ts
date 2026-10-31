import { expect, it } from "vitest";
import { route } from "./route";

it("is the incident page, reachable from links only (no nav entry)", () => {
  expect(route.path).toBe("/projects/:projectId/incidents/:incidentId");
  expect(route.nav).toBeUndefined();
});
