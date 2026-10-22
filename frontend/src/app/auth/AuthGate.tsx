import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Outlet } from "react-router";
import { SignIn } from "./SignIn";
import { exchangeTokenOnce, useAuth } from "./useAuth";
import { errorText } from "@/lib/api";

function extractTokenFromHash(hash: string): string | null {
  const match = hash.match(/(?:^|[#&])token=([^&]+)/);
  if (!match || !match[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    // A malformed token is sent as-is so the server answers bad_token.
    return match[1];
  }
}

export function AuthGate({ children }: { children?: ReactNode }) {
  const qc = useQueryClient();

  const initialToken = useMemo(() => {
    return extractTokenFromHash(window.location.hash);
  }, []);

  const [isExchanging, setIsExchanging] = useState(initialToken !== null);
  const [tokenError, setTokenError] = useState<string | null>(null);

  const { status, error } = useAuth();

  useEffect(() => {
    if (!initialToken) return;

    history.replaceState(
      window.history.state,
      "",
      window.location.pathname + window.location.search,
    );

    const state = { canceled: false };
    void (async () => {
      const result = await exchangeTokenOnce(initialToken);
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

  if (error && status === undefined) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <div role="alert" className="text-sm font-medium text-destructive">
          {errorText(error)}
        </div>
      </div>
    );
  }

  if (status === undefined) {
    return null;
  }

  if (!status.signed_in) {
    return <SignIn mode={status.mode} tokenError={tokenError} />;
  }

  return children ?? <Outlet />;
}
