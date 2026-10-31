import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Dashboard } from "./useDashboard";

export function ModelCards({ models }: Pick<Dashboard, "models">) {
  if (models.length === 0) return null;
  return (
    <section aria-labelledby="models-title" className="space-y-3">
      <h2 id="models-title" className="text-lg font-semibold">
        Models
      </h2>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {models.map((model) => (
          <Card key={model.name} size="sm" data-testid={`model-${model.name}`}>
            <CardHeader>
              <CardTitle className="break-words">{model.name}</CardTitle>
              <p className="text-xs text-muted-foreground">
                {typeof model.production_version === "string"
                  ? `Production version ${model.production_version}`
                  : "No production version"}
              </p>
            </CardHeader>
            <CardContent className="space-y-2">
              {Object.keys(model.latest_metrics).length === 0 ? (
                <p className="text-xs text-muted-foreground">No metrics yet</p>
              ) : (
                <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 text-xs">
                  {Object.entries(model.latest_metrics).map(([check, value]) => (
                    <div key={check} className="contents">
                      <dt className="truncate text-muted-foreground">{check}</dt>
                      <dd className="font-mono">{value}</dd>
                    </div>
                  ))}
                </dl>
              )}
              <p className="text-xs">
                {model.open_incidents} open {model.open_incidents === 1 ? "incident" : "incidents"}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}
