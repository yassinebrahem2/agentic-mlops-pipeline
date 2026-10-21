import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "./api";

function is401(error: unknown): boolean {
  if (error instanceof ApiError && error.status === 401) {
    return true;
  }
  if (error !== null && typeof error === "object") {
    if ("status" in error && error.status === 401) {
      return true;
    }
    if ("code" in error && error.code === "not_signed_in") {
      return true;
    }
  }
  return false;
}

export const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({
    onError(error) {
      if (is401(error)) {
        void queryClient.invalidateQueries({ queryKey: ["auth"] });
      }
    },
  }),
  mutationCache: new MutationCache({
    onError(error) {
      if (is401(error)) {
        void queryClient.invalidateQueries({ queryKey: ["auth"] });
      }
    },
  }),
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          return false;
        }
        return failureCount < 1;
      },
    },
  },
});
