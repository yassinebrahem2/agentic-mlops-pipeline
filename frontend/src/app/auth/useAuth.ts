import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import type { components } from "@/api/schema";
import { api, unwrap } from "@/lib/api";

export type AuthStatus = components["schemas"]["AuthStatus"];

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
      } finally {
        activeExchangeToken = null;
        activeExchangePromise = null;
      }
    })();
  }
  return activeExchangePromise;
}

export function useAuth(): {
  status: AuthStatus | undefined;
  error: unknown;
  signIn(password: string): Promise<AuthStatus>;
  signOut(): Promise<void>;
} {
  const qc = useQueryClient();

  const query = useQuery<AuthStatus>({
    queryKey: ["auth"],
    queryFn: async () => unwrap(await api.GET("/api/auth/status")),
  });

  const signIn = useCallback(
    async (password: string) => {
      const status = unwrap(
        await api.POST("/api/auth/login", {
          body: { password },
        }),
      );
      qc.setQueryData(["auth"], status);
      await qc.invalidateQueries({ queryKey: ["auth"] });
      return status;
    },
    [qc],
  );

  const signOut = useCallback(async () => {
    const updated = unwrap(await api.POST("/api/auth/logout"));
    qc.setQueryData(["auth"], updated);
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
  }, [qc]);

  return {
    status: query.data,
    error: query.error,
    signIn,
    signOut,
  };
}
