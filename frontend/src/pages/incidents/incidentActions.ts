import type { IncidentView } from "./labels";

type Actions = { canAcknowledge: boolean; canClose: boolean; canSnooze: boolean };

/**
 * Which person actions an incident still takes, for the Inbox row and the incident page alike.
 * It follows the fields the backend's guards depend on, so no status list is copied here:
 * a merge sets merged_into (never closed_at), a resolution sets resolved_at, and a reopen
 * clears it again.
 */
export function incidentActions(incident: IncidentView): Actions {
  const live = incident.closed_at === null && incident.merged_into === null;
  return {
    canAcknowledge: live && incident.acknowledged_at === null && incident.resolved_at === null,
    canClose: live,
    // The backend snoozes any incident it knows; a closed or merged one has nothing left to silence.
    canSnooze: live,
  };
}
