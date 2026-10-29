import type { components } from "@/api/schema";

export type ModelEntry = components["schemas"]["ModelEntry"];
export type ModelType = ModelEntry["model_type"];

export function hostedModelWarning(provider: string): string {
  return `Files the agent reads are sent to ${provider}. A secret inside a committed file whose name does not look secret can reach it.`;
}

export function hostedEmbedderWarning(provider: string): string {
  return `Note titles and text are sent to ${provider} to compute their vectors.`;
}

export function usableModels(models: ModelEntry[], type: ModelType): ModelEntry[] {
  return models.filter((m) => m.model_type === type && m.problem === null);
}
