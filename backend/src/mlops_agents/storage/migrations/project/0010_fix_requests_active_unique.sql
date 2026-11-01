-- One active fix per incident (#557).
CREATE UNIQUE INDEX fix_requests_active_incident ON fix_requests (incident_id) WHERE status IN ('requested', 'chosen', 'running');
