import { ChevronRight } from "lucide-react";
import { Fragment } from "react";
import type { Dashboard } from "./useDashboard";
import { layerStages } from "./pipelineLayers";
import { StageCard } from "./StageCard";

export function PipelineMap({ stages, edges }: Pick<Dashboard, "stages" | "edges">) {
  return (
    <section aria-labelledby="pipeline-map-title" className="space-y-3">
      <h2 id="pipeline-map-title" className="text-lg font-semibold">
        Pipeline
      </h2>
      {stages.length === 0 ? (
        <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
          The pipeline map appears once onboarding is approved.
        </div>
      ) : (
        <div role="region" aria-label="Pipeline map" className="space-y-3">
          <div className="flex items-stretch gap-2 overflow-x-auto pb-2">
            {layerStages(stages, edges).map((layer, index) => (
              <Fragment key={layer.map((s) => s.name).join("|")}>
                {index > 0 && (
                  <ChevronRight
                    className="size-5 shrink-0 self-center text-muted-foreground"
                    aria-hidden="true"
                  />
                )}
                <ul className="flex shrink-0 flex-col justify-center gap-2">
                  {layer.map((stage) => (
                    <StageCard key={stage.name} stage={stage} />
                  ))}
                </ul>
              </Fragment>
            ))}
          </div>
          {edges.length > 0 && (
            <ul
              aria-label="Pipeline edges"
              className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground"
            >
              {edges.map((edge) => (
                <li key={`${edge.from_stage}>${edge.to_stage}`}>
                  {edge.from_stage} feeds {edge.to_stage}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
