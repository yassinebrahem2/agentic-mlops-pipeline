import { useId, useState } from "react";
import { useParams } from "react-router";
import { AlertCircle, BarChart3, Info } from "lucide-react";
import type { components } from "@/api/schema";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorText } from "@/lib/api";
import {
  CATEGORY_LABELS,
  formatStat,
  groupLabel,
  isResultCategory,
  isStatsWindow,
  metricLabel,
  type MetricUnit,
  type StatsWindow,
  WINDOW_LABELS,
} from "./metricLabels";
import { SilentAlerts } from "./SilentAlerts";
import { type CategoryChoice, useStats } from "./useStats";

type StatValue = components["schemas"]["StatValue"];
type StatsView = components["schemas"]["StatsView"];

export function StatsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return null;
  return <StatsContent projectId={projectId} />;
}

function StatsContent({ projectId }: { projectId: string }) {
  const [statsWindow, setStatsWindow] = useState<StatsWindow>("30d");
  const [category, setCategory] = useState<CategoryChoice>("all");
  const stats = useStats(projectId, statsWindow, category);

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-6">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-semibold">
          <BarChart3 className="size-6 text-primary" />
          Stats
        </h1>
        <p className="text-sm text-muted-foreground">
          How the agents do against the team's own monitoring.
        </p>
      </div>

      <div className="flex flex-wrap gap-4">
        <Tabs
          value={statsWindow}
          onValueChange={(value) => {
            if (isStatsWindow(value)) setStatsWindow(value);
          }}
        >
          <TabsList aria-label="Time window">
            {Object.entries(WINDOW_LABELS).map(([value, label]) => (
              <TabsTrigger key={value} value={value}>
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <Tabs
          value={category}
          onValueChange={(value) => {
            setCategory(isResultCategory(value) ? value : "all");
          }}
        >
          <TabsList aria-label="Category">
            <TabsTrigger value="all">All categories</TabsTrigger>
            {Object.entries(CATEGORY_LABELS).map(([value, label]) => (
              <TabsTrigger key={value} value={value}>
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>

      {stats.isPending && <p className="text-sm text-muted-foreground">Loading stats…</p>}
      {stats.isError && (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertTitle>Error loading stats</AlertTitle>
          <AlertDescription>{errorText(stats.error)}</AlertDescription>
        </Alert>
      )}
      {stats.data && <Groups view={stats.data} />}

      <SilentAlerts projectId={projectId} />
    </div>
  );
}

function Groups({ view }: { view: StatsView }) {
  return (
    <>
      {!view.baseline_available && (
        <Alert variant="warning">
          <Info className="size-4" />
          <AlertTitle>No baseline yet</AlertTitle>
          <AlertDescription>
            The team's own monitoring is not readable yet, so comparisons with it are not available.
          </AlertDescription>
        </Alert>
      )}
      {Object.entries(view.groups).map(([group, metrics]) => (
        <Group key={group} name={group} metrics={metrics} />
      ))}
    </>
  );
}

function Group({ name, metrics }: { name: string; metrics: Record<string, StatValue> }) {
  const titleId = useId();
  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-3">
      <h2 id={titleId} className="text-lg font-semibold">
        {groupLabel(name)}
      </h2>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(metrics).map(([metric, stat]) => (
          <Stat key={metric} name={metric} stat={stat} />
        ))}
      </div>
    </section>
  );
}

function Stat({ name, stat }: { name: string; stat: StatValue }) {
  const { label, unit } = metricLabel(name);
  const basedOn = `based on ${String(stat.n)}`;

  return (
    <div role="group" aria-label={label} className="flex flex-col gap-1 rounded-lg border p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <StatBody stat={stat} unit={unit} basedOn={basedOn} />
    </div>
  );
}

function StatBody({ stat, unit, basedOn }: { stat: StatValue; unit: MetricUnit; basedOn: string }) {
  const { value } = stat;
  // Collecting wins over a missing value: below the minimum verdicts (zero included) the
  // number is on its way, and the API alone says when that is.
  if (stat.collecting) {
    return <p className="text-sm text-muted-foreground">collecting data ({basedOn})</p>;
  }
  if (value === null || value === undefined) {
    return (
      <>
        <p className="text-2xl font-semibold text-muted-foreground">not available</p>
        <p className="text-xs text-muted-foreground">{basedOn}</p>
      </>
    );
  }
  return (
    <>
      <p className="text-2xl font-semibold">{formatStat(value, unit)}</p>
      <p className="text-xs text-muted-foreground">{basedOn}</p>
    </>
  );
}
