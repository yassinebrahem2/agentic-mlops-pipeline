import type { components } from "@/api/schema";

export type DataRole = components["schemas"]["DataRole"];
export type HandoffView = components["schemas"]["HandoffView"];

export function roleFor(
  handoff: HandoffView | null,
  connectionName: string,
  table: string,
): DataRole | null {
  if (!handoff) {
    return null;
  }
  return (
    handoff.payload.data_roles.find(
      (entry) => entry.connection === connectionName && entry.table === table,
    ) ?? null
  );
}

export function modelsUsing(
  handoff: HandoffView | null,
  connectionName: string,
  table: string,
): { model: string; as: "predictions" | "labels" }[] {
  if (!handoff) {
    return [];
  }
  const result: { model: string; as: "predictions" | "labels" }[] = [];
  for (const model of handoff.payload.models) {
    if (model.predictions?.connection === connectionName && model.predictions.table === table) {
      result.push({ model: model.name, as: "predictions" });
    }
    if (model.labels?.connection === connectionName && model.labels.table === table) {
      result.push({ model: model.name, as: "labels" });
    }
  }
  return result;
}
