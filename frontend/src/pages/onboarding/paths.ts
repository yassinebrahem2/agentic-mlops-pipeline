import { generatePath } from "react-router";

// The Onboarding step's path lives here so the route and the add-project tabs that lead to it share it.
export const ONBOARDING_PATH = "/projects/:projectId/onboarding";

export function onboardingPath(projectId: string): string {
  return generatePath(ONBOARDING_PATH, { projectId });
}
