import { expect, it } from "vitest";
import { relativeAge } from "./age";

const NOW = new Date("2026-10-30T12:00:00Z").getTime();

it.each([
  ["2026-10-30T11:59:40Z", "just now"],
  ["2026-10-30T11:55:00Z", "5 min ago"],
  ["2026-10-30T09:00:00Z", "3 h ago"],
  ["2026-10-27T12:00:00Z", "3 d ago"],
  ["not a date", "–"],
])("formats %s as %s", (iso, expected) => {
  expect(relativeAge(iso, NOW)).toBe(expected);
});
