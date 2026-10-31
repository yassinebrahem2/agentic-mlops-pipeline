import { describe, expect, it } from "vitest";
import { layerStages } from "./pipelineLayers";

const stage = (name: string) => ({ name });
const edge = (from_stage: string, to_stage: string) => ({ from_stage, to_stage });

function names(layers: { name: string }[][]): string[][] {
  return layers.map((layer) => layer.map((s) => s.name));
}

describe("layerStages", () => {
  it("puts stages in the order the edges lead", () => {
    const layers = layerStages(
      [stage("train"), stage("ingest"), stage("prepare")],
      [edge("ingest", "prepare"), edge("prepare", "train")],
    );
    expect(names(layers)).toEqual([["ingest"], ["prepare"], ["train"]]);
  });

  it("puts parallel branches in one layer and joins after the longest path", () => {
    const layers = layerStages(
      [stage("a"), stage("b"), stage("c"), stage("d")],
      [edge("a", "b"), edge("a", "c"), edge("b", "d"), edge("c", "d")],
    );
    expect(names(layers)).toEqual([["a"], ["b", "c"], ["d"]]);
  });

  it("keeps the pipeline order inside a layer and puts unconnected stages first", () => {
    const layers = layerStages([stage("x"), stage("a"), stage("b")], [edge("a", "b")]);
    expect(names(layers)).toEqual([["x", "a"], ["b"]]);
  });

  it("lays out a feedback cycle once, without looping", () => {
    const layers = layerStages(
      [stage("train"), stage("serve")],
      [edge("train", "serve"), edge("serve", "train")],
    );
    expect(names(layers)).toEqual([["train"], ["serve"]]);
  });

  it("has no layers without stages", () => {
    expect(layerStages([], [])).toEqual([]);
  });
});
