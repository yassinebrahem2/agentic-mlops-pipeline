import type { components } from "@/api/schema";

type Edge = components["schemas"]["PipelineEdge"];

/**
 * Groups stages into columns, left to right, so every edge leads rightwards: a stage sits one
 * column after the longest chain of stages feeding it. Pipelines may loop (predictions feed
 * training again), so an edge that closes a loop is left out of the layout (it is still listed
 * as an edge). Inside a column the pipeline's own order is kept.
 */
export function layerStages<S extends { name: string }>(stages: S[], edges: Edge[]): S[][] {
  const names = new Set(stages.map((s) => s.name));
  const outgoing = new Map<string, string[]>();
  const hasIncoming = new Set<string>();
  for (const edge of edges) {
    if (!names.has(edge.from_stage) || !names.has(edge.to_stage)) continue;
    outgoing.set(edge.from_stage, [...(outgoing.get(edge.from_stage) ?? []), edge.to_stage]);
    hasIncoming.add(edge.to_stage);
  }

  // Depth-first from the stages nothing feeds, then from whatever is left (a pure loop): an
  // edge into a stage still on the walk's path closes a loop.
  const forward = new Map<string, string[]>();
  const state = new Map<string, "walking" | "done">();
  const walk = (name: string): void => {
    state.set(name, "walking");
    for (const next of outgoing.get(name) ?? []) {
      if (state.get(next) === "walking") continue;
      forward.set(next, [...(forward.get(next) ?? []), name]);
      if (!state.has(next)) walk(next);
    }
    state.set(name, "done");
  };
  for (const stage of stages)
    if (!hasIncoming.has(stage.name) && !state.has(stage.name)) walk(stage.name);
  for (const stage of stages) if (!state.has(stage.name)) walk(stage.name);

  const layerOf = new Map<string, number>();
  const layerFor = (name: string): number => {
    const known = layerOf.get(name);
    if (known !== undefined) return known;
    const layer = Math.max(-1, ...(forward.get(name) ?? []).map(layerFor)) + 1;
    layerOf.set(name, layer);
    return layer;
  };

  const layers: S[][] = [];
  for (const stage of stages) {
    const index = layerFor(stage.name);
    layers[index] = [...(layers[index] ?? []), stage];
  }
  return layers;
}
