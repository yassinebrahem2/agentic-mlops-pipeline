import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Outlet } from "react-router";
import { SignIn } from "./SignIn";
import { exchangeTokenOnce, useAuth } from "./useAuth";
import { errorText } from "@/lib/api";

const BAD_LINK_MESSAGE = "The access link is invalid or has expired.";

type ExtractedToken = { token: string } | { bad: true } | null;

function extractTokenFromHash(hash: string): ExtractedToken {
  const match = hash.match(/(?:^|[#&])token=([^&]+)/);
  if (!match || !match[1]) return null;
  try {
    return { token: decodeURIComponent(match[1]) };
  } catch {
    return { bad: true };
  }
}

export function AuthGate({ children }: { children?: ReactNode }) {
  const qc = useQueryClient();

  const initialToken = useMemo(() => {
    return extractTokenFromHash(window.location.hash);
  }, []);

  const [isExchanging, setIsExchanging] = useState(
    initialToken !== null && "token" in initialToken,
  );
  const [tokenError, setTokenError] = useState<string | null>(
    initialToken !== null && "bad" in initialToken ? BAD_LINK_MESSAGE : null,
  );

  const { status, error } = useAuth();

  useEffect(() => {
    if (!initialToken) return;

    history.replaceState(
      window.history.state,
      "",
      window.location.pathname + window.location.search,
    );

    if ("bad" in initialToken) {
      return;
    }

    const state = { canceled: false };
    void (async () => {
      const result = await exchangeTokenOnce(initialToken.token);
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
