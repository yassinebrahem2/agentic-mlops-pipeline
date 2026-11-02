-- Mark rows from a tracking source's first sync as baseline (#649).
-- Baseline rows describe starting state (where aliases already were), not moves.
-- Existing rows in upgraded databases default to 0 (not baseline).
ALTER TABLE deployments ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0;
