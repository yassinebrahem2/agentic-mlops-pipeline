import type { components } from "@/api/schema";

export type Cadence = components["schemas"]["Cadence"];

export function formatCadence(cadence: Cadence | null | undefined): string {
  if (!cadence) return "none";
  if (cadence.cron) return cadence.cron;
  if (cadence.every_minutes != null) return `every ${String(cadence.every_minutes)} minutes`;
  return "none";
}
