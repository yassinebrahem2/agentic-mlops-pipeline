import { expect, test } from "vitest";
import { PROJECT_EVENT_TYPES, type ProjectEventType } from "@/lib/useProjectEvents";

// A backend event type missing from PROJECT_EVENT_TYPES makes this a compile error (tsc -b in
// npm run build), so the two lists cannot drift apart.
type Missing = Exclude<ProjectEventType, (typeof PROJECT_EVENT_TYPES)[number]>;
const everyTypeListed: [Missing] extends [never] ? true : false = true;

test("PROJECT_EVENT_TYPES lists every backend event type", () => {
  expect(everyTypeListed).toBe(true);
  expect(new Set(PROJECT_EVENT_TYPES).size).toBe(PROJECT_EVENT_TYPES.length);
});
