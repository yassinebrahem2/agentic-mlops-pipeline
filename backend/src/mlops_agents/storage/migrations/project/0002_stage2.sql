-- messages gains 'waiting' in its status CHECK (SQLite cannot alter a CHECK) and the
-- exchange columns, so it is rebuilt. No table references messages, so the DROP is safe with
-- foreign keys on. Index names are global: the indexes are recreated after the rename.
-- exchanges comes first so the rebuilt messages.exchange_id can reference it.
CREATE TABLE exchanges (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE messages_new (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
  turn_id TEXT REFERENCES turns (id) ON DELETE SET NULL,
  author_kind TEXT NOT NULL CHECK (author_kind IN ('user', 'agent', 'system')),
  author_name TEXT NOT NULL,
  from_agent TEXT,
  parts TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('complete', 'streaming', 'queued', 'stopped', 'interrupted', 'waiting')),
  created_at TEXT NOT NULL,
  exchange_id TEXT REFERENCES exchanges (id) ON DELETE SET NULL,
  hop INTEGER,
  read_by_turn TEXT REFERENCES turns (id) ON DELETE SET NULL
);

INSERT INTO messages_new (
  seq, id, session_id, turn_id, author_kind, author_name, from_agent, parts, status, created_at
)
SELECT
  seq, id, session_id, turn_id, author_kind, author_name, from_agent, parts, status, created_at
FROM messages;

-- AUTOINCREMENT keeps a high-water mark: carry it over so deleted seq values are never reused.
DELETE FROM sqlite_sequence WHERE name = 'messages_new';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'messages_new', seq FROM sqlite_sequence WHERE name = 'messages';

DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;
CREATE INDEX messages_session_seq ON messages (session_id, seq);
CREATE INDEX messages_exchange ON messages (exchange_id);
CREATE INDEX messages_read_by_turn ON messages (read_by_turn);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
  agent TEXT NOT NULL,
  title TEXT NOT NULL,
  branch TEXT NOT NULL,
  worktree TEXT NOT NULL,
  base_ref TEXT NOT NULL,
  base_sha TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('active', 'in_review', 'merged', 'closed', 'discarded')),
  review_kind TEXT CHECK (review_kind IN ('github', 'local')),
  pr_number INTEGER,
  pr_url TEXT,
  created_at TEXT NOT NULL,
  ended_at TEXT
);

CREATE INDEX tasks_session_status ON tasks (session_id, status);

CREATE TABLE task_reads (
  task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  PRIMARY KEY (task_id, path)
);

CREATE TABLE file_claims (
  task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  PRIMARY KEY (task_id, path)
);

CREATE INDEX file_claims_path ON file_claims (path);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  tool_call_id TEXT NOT NULL UNIQUE,
  tool TEXT NOT NULL,
  risk_class TEXT NOT NULL,
  preview TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'expired')),
  note TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);

CREATE INDEX approvals_status ON approvals (status);

CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  type TEXT NOT NULL,
  subject_id TEXT,
  payload TEXT NOT NULL
);

CREATE INDEX events_type ON events (type);

-- Every column is NOT NULL, as I11's AuditRecord requires. SQLite lets a non-integer PRIMARY
-- KEY hold NULL unless it says NOT NULL, so id says it too.
CREATE TABLE audit (
  id TEXT PRIMARY KEY NOT NULL,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  risk_class TEXT NOT NULL,
  permission_level TEXT NOT NULL,
  decision TEXT NOT NULL,
  target TEXT NOT NULL,
  detail TEXT NOT NULL
);

CREATE TABLE wakeups (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
  agent TEXT NOT NULL,
  note TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled')),
  created_at TEXT NOT NULL,
  fired_at TEXT
);

CREATE INDEX wakeups_status_due ON wakeups (status, due_at);
