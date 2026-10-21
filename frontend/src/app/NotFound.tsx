import { Link } from "react-router";

export function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center p-12 text-center">
      <h1 className="text-2xl font-bold tracking-tight">This page does not exist.</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        The link you followed may be broken, or the page may have been removed.
      </p>
      <Link
        to="/"
        className="mt-4 inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
      >
        Home
      </Link>
    </div>
  );
}
