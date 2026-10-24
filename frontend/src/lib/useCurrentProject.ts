import { useEffect } from "react";
import { useLocation, useParams } from "react-router";
import { useProjects } from "@/lib/useProjects";

// App routes carry no :projectId, so the project last seen in a project route is kept here for
// the rest of the page load. Module state, not React state: the sidebar and the switcher both
// read it and it must outlive the project routes unmounting.
let lastProjectId: string | undefined;

export function resetCurrentProjectStore(): void {
  lastProjectId = undefined;
}

export function useCurrentProject() {
  const { projectId: urlProjectId } = useParams<{ projectId?: string }>();
  const { pathname } = useLocation();
  const { data: projects } = useProjects();

  useEffect(() => {
    if (urlProjectId) lastProjectId = urlProjectId;
  }, [urlProjectId]);

  // The home page lists projects itself, so no project is current there.
  const isHome = pathname === "/";
  if (urlProjectId) {
    return {
      projectId: urlProjectId,
      project: projects?.find((p) => p.id === urlProjectId),
    };
  }
  if (isHome) return { projectId: undefined, project: undefined };

  const remembered = projects?.find((p) => p.id === lastProjectId);
  // A remembered project that was removed since is no longer current, so fall back like a fresh load.
  const project = remembered ?? latestOpened(projects);
  return { projectId: project?.id, project };
}

function latestOpened<T extends { last_opened_at: string | null }>(
  projects: T[] | undefined,
): T | undefined {
  let latest: T | undefined;
  for (const p of projects ?? []) {
    if (!p.last_opened_at) continue;
    if (!latest?.last_opened_at || p.last_opened_at > latest.last_opened_at) latest = p;
  }
  return latest;
}
