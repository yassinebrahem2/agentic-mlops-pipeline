import { useQueryClient } from "@tanstack/react-query";
import { useState, type SyntheticEvent } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { APP_NAME } from "@/config";
import { api, errorText, unwrap } from "@/lib/api";

export interface SignInProps {
  mode: "local" | "server";
  tokenError?: string | null;
  onSuccess?: () => void;
}

export function SignIn({ mode, tokenError, onSuccess }: SignInProps) {
  const qc = useQueryClient();

  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: SyntheticEvent<HTMLFormElement>) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const status = unwrap(
        await api.POST("/api/auth/login", {
          body: { password },
        }),
      );
      qc.setQueryData(["auth"], status);
      await qc.invalidateQueries({ queryKey: ["auth"] });
      onSuccess?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-xl">{APP_NAME}</CardTitle>
          <CardDescription>
            {mode === "local" ? "Local mode access" : "Sign in to continue"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {mode === "local" ? (
            <div className="space-y-4">
              {tokenError && (
                <div role="alert" className="text-sm font-medium text-destructive">
                  {tokenError}
                </div>
              )}
              <p className="text-sm text-muted-foreground">
                Open the app with the link printed in the terminal where it runs.
              </p>
            </div>
          ) : (
            <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
              {error && (
                <div role="alert" className="text-sm font-medium text-destructive">
                  {error}
                </div>
              )}
              <div className="space-y-2">
                <Label htmlFor="password">Password</Label>
                <Input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => {
                    setPassword(e.target.value);
                  }}
                  autoComplete="current-password"
                  required
                />
              </div>
              <Button type="submit" className="w-full" disabled={submitting}>
                Sign in
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
