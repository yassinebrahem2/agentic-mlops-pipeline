import { Alert, AlertDescription } from "@/components/ui/alert";
import { errorText, fieldErrors } from "@/lib/api";

/** A refused action: the server's message as is, then the fields it names (a 422's detail). */
export function ActionError({ error }: { error: unknown }) {
  if (!error) return null;
  const fields = Object.entries(fieldErrors(error));
  return (
    <Alert variant="destructive">
      <AlertDescription>
        <p>{errorText(error)}</p>
        {fields.length > 0 && (
          <ul className="list-disc pl-4">
            {fields.map(([field, message]) => (
              <li key={field}>{`${field}: ${message}`}</li>
            ))}
          </ul>
        )}
      </AlertDescription>
    </Alert>
  );
}
