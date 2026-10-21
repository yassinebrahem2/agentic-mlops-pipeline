import { QueryClientContext } from "@tanstack/react-query";
import { useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Outlet } from "react-router";
import { SignIn } from "./SignIn";
import { exchangeTokenOnce, useAuth } from "./useAuth";
import { errorText } from "@/lib/api";
import { queryClient as fallbackClient } from "@/lib/query";

function extractTokenFromHash(hash: string): string | null {
  const match = hash.match(/token=([^&]+)/);
  return match && match[1] ? decodeURIComponent(match[1]) : null;
}

export function AuthGate({ children }: { children?: ReactNode }) {
  const contextClient = useContext(QueryClientContext);
  const qc = contextClient ?? fallbackClient;

  const initialToken = useMemo(() => {
    if (typeof window === "undefined") return null;
    return extractTokenFromHash(window.location.hash);
  }, []);

  const [isExchanging, setIsExchanging] = useState(initialToken !== null);
  const [tokenError, setTokenError] = useState<string | null>(null);

  const { status } = useAuth();

  useEffect(() => {
    if (!initialToken) return;

    const state = { canceled: false };
    void (async () => {
      const result = await exchangeTokenOnce(initialToken);
      if (typeof window !== "undefined") {
        history.replaceState(null, "", window.location.pathname + window.location.search);
      }
      if (state.canceled) return;

      if ("status" in result) {
        qc.setQueryData(["auth"], result.status);
        await qc.invalidateQueries({ queryKey: ["auth"] });
      } else {
        setTokenError(errorText(result.error));
        await qc.invalidateQueries({ queryKey: ["auth"] });
      }
      setIsExchanging(false);
    })();

    return () => {
      state.canceled = true;
    };
  }, [initialToken, qc]);

  if (isExchanging) {
    return null;
  }

  if (status === undefined) {
    return null;
  }

  if (!status.signed_in) {
    return <SignIn mode={status.mode} tokenError={tokenError} />;
  }

  return children ?? <Outlet />;
}
