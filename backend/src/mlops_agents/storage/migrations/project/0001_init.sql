CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  agent TEXT NOT NULL UNIQUE,
  summary TEXT,
  compacted_through TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE turns (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
  trigger_message_id TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('queued', 'running', 'waiting', 'done', 'stopped', 'interrupted', 'failed')),
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT
);

CREATE INDEX turns_session_status ON turns (session_id, status);

CREATE TABLE messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
  turn_id TEXT REFERENCES turns (id) ON DELETE SET NULL,
  author_kind TEXT NOT NULL CHECK (author_kind IN ('user', 'agent', 'system')),
  author_name TEXT NOT NULL,
  from_agent TEXT,
  parts TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('complete', 'streaming', 'queued', 'stopped', 'interrupted')),
  created_at TEXT NOT NULL
);

CREATE INDEX messages_session_seq ON messages (session_id, seq);

CREATE TABLE llm_calls (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
  agent TEXT NOT NULL,
  model_address TEXT NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  latency_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE project_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
