import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";

interface QueryLike {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => unknown;
}

/** The loading line, or the server's error with a Retry; renders nothing once data is there. */
export function QueryStatus({ query, loading }: { query: QueryLike; loading: string }) {
  if (query.isPending) return <p role="status">{loading}</p>;
  if (!query.isError) return null;
  return (
    <div className="flex flex-wrap items-center gap-3">
      <p role="alert" className="text-sm text-destructive">
        {errorText(query.error)}
      </p>
      <Button
        variant="outline"
        size="sm"
        onClick={() => {
          void query.refetch();
        }}
      >
        Retry
      </Button>
    </div>
  );
}
