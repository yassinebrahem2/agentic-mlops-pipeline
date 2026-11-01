import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

/** A saved report is immutable, so it is fetched once, when asked for, and needs no event refetch. */
export function useReport(projectId: string, reportId: string | undefined) {
  return useQuery({
    queryKey: ["projects", projectId, "reports", reportId],
    queryFn: async () => {
      if (!reportId) throw new Error("No reportId");
      return unwrap(
        await api.GET("/api/projects/{project_id}/reports/{report_id}", {
          params: { path: { project_id: projectId, report_id: reportId } },
        }),
      );
    },
    enabled: Boolean(reportId),
  });
}
