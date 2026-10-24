import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { api, errorText, unwrap } from "@/lib/api";
import { projectsKey } from "@/lib/useProjects";

export interface UseOpenProjectOptions {
  onNavigate?: () => void;
}

export function useOpenProject(options?: UseOpenProjectOptions) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: async (targetId: string) => {
      const res = await api.POST("/api/projects/{project_id}/open", {
        params: { path: { project_id: targetId } },
      });
      return unwrap(res);
    },
    onSuccess: (_data, targetId) => {
      void queryClient.invalidateQueries({ queryKey: projectsKey });
      options?.onNavigate?.();
      void navigate(`/projects/${targetId}`);
    },
  });

  return {
    openProject: (id: string) => {
      mutation.mutate(id);
    },
    openingId: mutation.isPending ? mutation.variables : null,
    error: mutation.error ? errorText(mutation.error) : null,
    errorProjectId: mutation.isError ? mutation.variables : null,
    clearError: () => {
      mutation.reset();
    },
  };
}
