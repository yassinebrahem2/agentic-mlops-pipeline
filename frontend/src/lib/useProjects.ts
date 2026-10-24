import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export const projectsKey = ["projects"] as const;

export function useProjects() {
  return useQuery({
    queryKey: projectsKey,
    queryFn: async () => unwrap(await api.GET("/api/projects")),
  });
}
