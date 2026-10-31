import { Alert, AlertDescription } from "@/components/ui/alert";
import { errorText } from "@/lib/api";

/** A refused action: the server's message is shown as is. */
export function ActionError({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <Alert variant="destructive">
      <AlertDescription>{errorText(error)}</AlertDescription>
    </Alert>
  );
}
