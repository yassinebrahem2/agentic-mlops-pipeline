-- Rebuild approvals table without UNIQUE on tool_call_id, and with task_id column.
CREATE TABLE approvals_new (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  risk_class TEXT NOT NULL,
  preview TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'expired')),
  note TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  task_id TEXT
);

INSERT INTO approvals_new (
  id, turn_id, session_id, message_id, agent, tool_call_id, tool,
  risk_class, preview, status, note, created_at, decided_at, task_id
)
SELECT
  id, turn_id, session_id, message_id, agent, tool_call_id, tool,
  risk_class, preview, status, note, created_at, decided_at, NULL
FROM approvals;

DROP TABLE approvals;
ALTER TABLE approvals_new RENAME TO approvals;

CREATE INDEX approvals_status ON approvals (status);
CREATE INDEX approvals_turn_id ON approvals (turn_id);
