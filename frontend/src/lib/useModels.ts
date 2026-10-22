import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export function useModels() {
  return useQuery({
    queryKey: ["models"],
    queryFn: async () => unwrap(await api.GET("/api/models")),
  });
}
