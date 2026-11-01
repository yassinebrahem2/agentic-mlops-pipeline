-- One active fix per incident (#557).
UPDATE fix_requests
SET status = 'declined'
WHERE status IN ('requested', 'chosen', 'running')
  AND id NOT IN (
    SELECT id FROM (
      SELECT id, ROW_NUMBER() OVER (
        PARTITION BY incident_id
        ORDER BY created DESC, id DESC
      ) AS rn
      FROM fix_requests
      WHERE status IN ('requested', 'chosen', 'running')
    )
    WHERE rn = 1
  );

CREATE UNIQUE INDEX fix_requests_active_incident ON fix_requests (incident_id) WHERE status IN ('requested', 'chosen', 'running');
