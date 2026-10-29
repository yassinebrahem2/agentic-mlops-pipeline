import type { components } from "@/api/schema";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { cn } from "@/lib/utils";

export type Findings = components["schemas"]["Findings"];
export type Finding = components["schemas"]["Finding"];

export interface ConnectionFindingsProps {
  findings: Findings;
  className?: string;
}

export function ConnectionFindings({ findings, className }: ConnectionFindingsProps) {
  const hasSecrets = findings.secret_files.length > 0;
  const hasUnsupported = findings.unsupported_files.length > 0;

  if (!hasSecrets && !hasUnsupported) {
    return null;
  }

  return (
    <div className={cn("space-y-4", className)}>
      {hasSecrets && (
        <Alert variant="destructive" className="space-y-2">
          <AlertTitle>Secret files</AlertTitle>
          <AlertDescription>
            The agents will be able to read these files; remove the connection to stop that.
          </AlertDescription>
          <ul className="list-disc list-inside space-y-1 text-sm font-mono">
            {findings.secret_files.map((file) => (
              <li key={file.path}>
                <span className="font-semibold">{file.path}</span>
                <span className="text-muted-foreground ml-2">{file.reason}</span>
              </li>
            ))}
          </ul>
        </Alert>
      )}

      {hasUnsupported && (
        <div className="space-y-2">
          <h4 className="text-sm font-medium text-muted-foreground">Unsupported files</h4>
          <ul className="list-disc list-inside space-y-1 text-sm">
            {findings.unsupported_files.map((file) => (
              <li key={file.path}>
                <span className="font-semibold font-mono">{file.path}</span>
                <span className="text-muted-foreground ml-2">{file.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {findings.truncated && <p className="text-xs text-muted-foreground italic">and more</p>}
    </div>
  );
}
