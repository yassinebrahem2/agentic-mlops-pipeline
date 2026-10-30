import { Link } from "react-router";
import type { components } from "@/api/schema";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { errorText } from "@/lib/api";
import { useProjects } from "@/lib/useProjects";
import { formatTokens, formatUsd } from "./format";
import { useUsage } from "./useUsage";

type UsageView = components["schemas"]["UsageView"];

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border p-4">
      <p className="text-muted-foreground text-sm">{label}</p>
      <p className="text-2xl font-semibold">{value}</p>
    </div>
  );
}

function Totals({ view }: { view: UsageView }) {
  const tokens = view.rows.reduce((sum, r) => sum + r.prompt_tokens + r.completion_tokens, 0);
  // A missing price is never a zero: only priced rows add to the dollar total.
  const priced = view.rows.filter((r) => r.cost_usd !== null && r.cost_usd !== undefined);
  const cost = priced.reduce((sum, r) => sum + (r.cost_usd ?? 0), 0);
  const tokensOnly = priced.length === 0;
  const partial = !tokensOnly && priced.length < view.rows.length;

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-4 sm:grid-cols-2">
        <Stat label="Total tokens" value={formatTokens(tokens)} />
        {!tokensOnly && <Stat label="Total cost" value={formatUsd(cost)} />}
      </div>
      {tokensOnly && (
        <p className="text-muted-foreground">Tokens only: no model used here has a price yet.</p>
      )}
      {partial && (
        <p className="text-muted-foreground">
          This total is partial: it leaves out the calls of models without a price.
        </p>
      )}
      {view.unpriced_models.length > 0 && (
        <p className="text-muted-foreground">
          No price set for: <span className="font-mono">{view.unpriced_models.join(", ")}</span>.
          Set one in{" "}
          <Link to="/providers" className="underline">
            AI providers
          </Link>
          .
        </p>
      )}
    </div>
  );
}

export function UsagePage() {
  const usage = useUsage();
  const projects = useProjects();
  const names = new Map((projects.data ?? []).map((p) => [p.id, p.name]));

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-6">
      <h1 className="text-2xl font-semibold">Usage</h1>
      {usage.isPending && <p className="text-muted-foreground">Loading usage…</p>}
      {usage.isError && <p className="text-destructive">{errorText(usage.error)}</p>}
      {usage.data?.rows.length === 0 && (
        <p className="text-muted-foreground">
          No model calls yet. Tokens and costs appear here once an agent has answered.
        </p>
      )}
      {usage.data && usage.data.rows.length > 0 && (
        <>
          <Totals view={usage.data} />
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Week starting</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>Agent</TableHead>
                <TableHead>Model</TableHead>
                <TableHead className="text-right">Prompt tokens</TableHead>
                <TableHead className="text-right">Completion tokens</TableHead>
                <TableHead className="text-right">Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {usage.data.rows.map((row) => (
                <TableRow
                  key={`${row.week_start}|${row.project_id}|${row.agent}|${row.model_address}`}
                >
                  <TableCell>{row.week_start}</TableCell>
                  <TableCell>{names.get(row.project_id) ?? row.project_id}</TableCell>
                  <TableCell>{row.agent}</TableCell>
                  <TableCell className="font-mono">{row.model_address}</TableCell>
                  <TableCell className="text-right">{formatTokens(row.prompt_tokens)}</TableCell>
                  <TableCell className="text-right">
                    {formatTokens(row.completion_tokens)}
                  </TableCell>
                  <TableCell className="text-right">
                    {row.cost_usd === null || row.cost_usd === undefined
                      ? "–"
                      : formatUsd(row.cost_usd)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </div>
  );
}
