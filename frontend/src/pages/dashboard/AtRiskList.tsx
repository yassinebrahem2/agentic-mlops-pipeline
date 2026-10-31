import { Badge } from "@/components/ui/badge";
import { formatTime } from "@/lib/format";
import type { Dashboard } from "./useDashboard";

export function AtRiskList({ items }: { items: Dashboard["at_risk"] }) {
  return (
    <section aria-labelledby="at-risk-title" className="space-y-3">
      <h2 id="at-risk-title" className="text-lg font-semibold">
        At risk
      </h2>
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing is due, late or silent.</p>
      ) : (
        <ul className="space-y-2">
          {items.map((item) => (
            <li
              key={`${item.key.kind}-${item.key.ref}-${item.key.model ?? ""}`}
              data-testid={`at-risk-${item.key.kind}-${item.key.ref}`}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3 text-sm"
            >
              <div className="min-w-0">
                <p className="font-medium break-words">{item.key.ref}</p>
                <p className="text-xs text-muted-foreground">
                  {item.key.kind}
                  {item.key.model ? ` of ${item.key.model}` : ""}
                  {item.next_deadline ? `, expected by ${formatTime(item.next_deadline)}` : ""}
                </p>
              </div>
              <Badge variant={item.status === "due" ? "outline" : "destructive"}>
                {item.status}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
