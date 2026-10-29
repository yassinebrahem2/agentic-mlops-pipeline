-- Who decided an approval, and whether its records (audit, event) and its card are written. An
-- approval decided but not settled is one whose decision a crash interrupted: the repair finds
-- it through approvals_unsettled, which holds only those rows.
ALTER TABLE approvals ADD COLUMN decided_by TEXT
  CHECK (decided_by IS NULL OR decided_by IN ('user', 'system'));
ALTER TABLE approvals ADD COLUMN settled_at TEXT;
-- Already-decided rows are settled as far as anyone knows: the first start does not re-settle
-- the whole history. decided_by stays NULL for them (unknown).
UPDATE approvals SET settled_at = COALESCE(decided_at, created_at) WHERE status <> 'pending';
CREATE INDEX approvals_unsettled ON approvals (created_at)
  WHERE status <> 'pending' AND settled_at IS NULL;
-- The settle checks: an approval's approval.decided event, and its audit record. A query uses
-- the partial expression index only when it writes action = 'approval' and the json_extract
-- path as literal text.
CREATE INDEX events_type_subject ON events (type, subject_id);
CREATE INDEX audit_approval_id ON audit (json_extract(detail, '$.approval_id'))
  WHERE action = 'approval';
