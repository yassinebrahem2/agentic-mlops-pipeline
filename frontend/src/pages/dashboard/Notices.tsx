import { Info } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { Dashboard } from "./useDashboard";

export function Notices({
  heartbeatReachable,
  baselineAvailable,
}: {
  heartbeatReachable: Dashboard["heartbeat_reachable"];
  baselineAvailable: Dashboard["baseline_available"];
}) {
  return (
    <>
      {!heartbeatReachable && (
        <Alert>
          <Info className="size-4" />
          <AlertTitle>Running locally</AlertTitle>
          <AlertDescription>
            The heartbeat cannot be reached from outside this machine, so an app that is down pages
            nobody. Run the app in server mode to be paged when it stops.
          </AlertDescription>
        </Alert>
      )}
      {!baselineAvailable && (
        <Alert>
          <Info className="size-4" />
          <AlertTitle>Your team's monitoring</AlertTitle>
          <AlertDescription>
            Your team's own monitoring is not readable yet, so comparisons with it are unavailable.
          </AlertDescription>
        </Alert>
      )}
    </>
  );
}
