import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useNavigate } from "react-router";
import { api, errorText, unwrap } from "@/lib/api";

export interface UseOpenProjectOptions {
  onNavigate?: () => void;
}

export function useOpenProject(options?: UseOpenProjectOptions) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorProjectId, setErrorProjectId] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async (targetId: string) => {
      const res = await api.POST("/api/projects/{project_id}/open", {
        params: { path: { project_id: targetId } },
      });
      return unwrap(res);
    },
    onSuccess: (_data, targetId) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      setOpeningId(null);
      setError(null);
      setErrorProjectId(null);
      options?.onNavigate?.();
      void navigate(`/projects/${targetId}`);
    },
    onError: (err, targetId) => {
      setOpeningId(null);
      setError(errorText(err));
      setErrorProjectId(targetId);
    },
  });

  const openProject = useCallback(
    (id: string) => {
      setError(null);
      setErrorProjectId(null);
      setOpeningId(id);
      mutation.mutate(id);
    },
    [mutation],
  );

  return {
    openProject,
    openingId,
    error,
    errorProjectId,
    clearError: useCallback(() => {
      setError(null);
      setErrorProjectId(null);
    }, []),
  };
}
