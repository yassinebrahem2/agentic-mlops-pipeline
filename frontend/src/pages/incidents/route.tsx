import type { PageRoute } from "@/app/routes";
import { IncidentPage } from "./IncidentPage";

// Reached by links (Inbox rows, Activity entries, notifications), so it has no nav entry.
// backend/tests/notifications/test_incident_path.py pins notifications' INCIDENT_PATH to this path.
export const route: PageRoute = {
  path: "/projects/:projectId/incidents/:incidentId",
  element: <IncidentPage />,
};
