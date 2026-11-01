import type { components } from "@/api/schema";
import { Badge } from "@/components/ui/badge";

type OpenPipelinePr = components["schemas"]["OpenPipelinePr"];

interface OpenPrsListProps {
  prs: OpenPipelinePr[];
  /** The view carries no PR URL, so the link is built from the project's repository; null for a local project. */
  githubFullName: string | null | undefined;
}

export function OpenPrsList({ prs, githubFullName }: OpenPrsListProps) {
  return (
    <ul className="space-y-3">
      {prs.map((pr) => {
        const text = `#${String(pr.number)} ${pr.title}`;
        return (
          <li
            key={pr.number}
            data-testid={`open-pr-${String(pr.number)}`}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border/80 bg-card p-3 text-sm shadow-xs"
          >
            {githubFullName ? (
              <a
                href={`https://github.com/${githubFullName}/pull/${String(pr.number)}`}
                target="_blank"
                rel="noreferrer"
                className="font-medium text-primary underline underline-offset-2"
              >
                {text}
              </a>
            ) : (
              <span className="font-medium">{text}</span>
            )}
            <span className="text-xs text-muted-foreground">{pr.agent}</span>
            {pr.labels.map((label) => (
              <Badge key={label} variant="outline">
                {label}
              </Badge>
            ))}
          </li>
        );
      })}
    </ul>
  );
}
