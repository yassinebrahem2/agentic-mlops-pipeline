-- Stage 4: turn stopped_reason, tasks CI wake tracking (#750, #748).
ALTER TABLE turns ADD COLUMN stopped_reason TEXT;
ALTER TABLE tasks ADD COLUMN ci_woken_sha TEXT;
ALTER TABLE tasks ADD COLUMN ci_wakes INTEGER NOT NULL DEFAULT 0;
