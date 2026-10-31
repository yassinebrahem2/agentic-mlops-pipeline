-- An expectation's registered_at: no slot before it is judged (#586). SQLite cannot add a
-- NOT NULL column without a constant default, so the table is rebuilt. Existing rows are
-- backfilled with the migration's time, so an upgraded project raises no silence for slots
-- from before the upgrade either.
CREATE TABLE expectations_new (
  kind TEXT NOT NULL CHECK (kind IN ('feed', 'job', 'check', 'training_request')),
  ref TEXT NOT NULL,
  model TEXT,
  spec TEXT NOT NULL,
  active INTEGER NOT NULL CHECK (active IN (0, 1)),
  last_seen TEXT,
  last_signalled_first TEXT,
  last_signalled_second TEXT,
  registered_at TEXT NOT NULL
);

INSERT INTO expectations_new (
  kind, ref, model, spec, active, last_seen, last_signalled_first, last_signalled_second,
  registered_at
)
SELECT
  kind, ref, model, spec, active, last_seen, last_signalled_first, last_signalled_second,
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM expectations;

DROP TABLE expectations;
ALTER TABLE expectations_new RENAME TO expectations;

CREATE UNIQUE INDEX expectations_key ON expectations (kind, ref, coalesce(model, ''));
