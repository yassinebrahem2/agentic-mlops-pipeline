import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { api, unwrap } from "@/lib/api";

export type AuthStatus = { mode: "local" | "server"; signed_in: boolean };

export type ExchangeResult = { status: AuthStatus } | { error: unknown };

let activeExchangePromise: Promise<ExchangeResult> | null = null;
let activeExchangeToken: string | null = null;

export function resetTokenExchange(): void {
  activeExchangePromise = null;
  activeExchangeToken = null;
}

export function exchangeTokenOnce(token: string): Promise<ExchangeResult> {
  if (activeExchangeToken !== token || !activeExchangePromise) {
    activeExchangeToken = token;
    activeExchangePromise = (async (): Promise<ExchangeResult> => {
      try {
        const res = unwrap(
          await api.POST("/api/auth/token", {
            body: { token },
          }),
        );
        return { status: res };
      } catch (error) {
        return { error };
      }
    })();
  }
  return activeExchangePromise;
}

export function useAuth(): {
  status: AuthStatus | undefined;
  error: unknown;
  signOut(): Promise<void>;
} {
  const qc = useQueryClient();

  const query = useQuery<AuthStatus>({
    queryKey: ["auth"],
    queryFn: async () => unwrap(await api.GET("/api/auth/status")),
  });

  const signOut = useCallback(async () => {
    const updated = unwrap(await api.POST("/api/auth/logout"));
    qc.setQueryData(["auth"], updated);
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
  }, [qc]);

  return {
    status: query.data,
    error: query.error,
    signOut,
  };
}
