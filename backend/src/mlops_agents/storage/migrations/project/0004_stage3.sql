-- Stage 3: connections, tracking records, knowledge notes, proposals and handoffs, the stall
-- approval kind. SQLite lets a non-integer PRIMARY KEY hold NULL unless it says NOT NULL, so
-- every text key and every part of a composite key says it.

CREATE TABLE connections (
  id TEXT PRIMARY KEY NOT NULL,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  settings_json TEXT NOT NULL,
  credential_ref TEXT,
  status TEXT NOT NULL CHECK (status IN ('ok', 'unreachable')),
  status_detail TEXT,
  created TEXT NOT NULL,
  updated TEXT NOT NULL
);

-- No credential ref is stored: it is derived from the source ID (tracking_ref).
CREATE TABLE tracking_sources (
  id TEXT PRIMARY KEY NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('mlflow', 'github_actions')),
  settings TEXT NOT NULL,
  auth TEXT NOT NULL CHECK (auth IN ('none', 'credential')),
  last_sync TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL
);

-- The tracking record. Each table is keyed by its source and the ID at the source, so a sync
-- that sees a row again overwrites it instead of duplicating it.
CREATE TABLE tracked_models (
  source_id TEXT NOT NULL REFERENCES tracking_sources (id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  PRIMARY KEY (source_id, name)
);

CREATE TABLE model_versions (
  source_id TEXT NOT NULL REFERENCES tracking_sources (id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  version TEXT NOT NULL,
  run_key TEXT,
  aliases TEXT NOT NULL,
  created_at TEXT NOT NULL,
  evaluation TEXT NOT NULL DEFAULT 'absent'
    CHECK (evaluation IN ('present', 'absent', 'fetch_failed')),
  PRIMARY KEY (source_id, model, version)
);

-- started_at and ended_at, not start and end: those are SQL words.
CREATE TABLE tracked_runs (
  source_id TEXT NOT NULL REFERENCES tracking_sources (id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  experiment TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'other')),
  started_at TEXT,
  ended_at TEXT,
  params TEXT NOT NULL,
  tags TEXT NOT NULL,
  git_commit TEXT,
  datasets TEXT NOT NULL,
  PRIMARY KEY (source_id, key)
);

CREATE TABLE run_metrics (
  source_id TEXT NOT NULL REFERENCES tracking_sources (id) ON DELETE CASCADE,
  run_key TEXT NOT NULL,
  metric TEXT NOT NULL,
  step INTEGER NOT NULL,
  time TEXT NOT NULL,
  value REAL NOT NULL,
  PRIMARY KEY (source_id, run_key, metric, step, time)
);

CREATE INDEX run_metrics_metric_time ON run_metrics (source_id, metric, time);

-- to_version is NULL when the alias was removed.
CREATE TABLE deployments (
  source_id TEXT NOT NULL REFERENCES tracking_sources (id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  alias TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  from_version TEXT,
  to_version TEXT,
  PRIMARY KEY (source_id, model, alias, seen_at)
);

-- commit_sha, not commit: that is an SQL word.
CREATE TABLE ci_runs (
  source_id TEXT NOT NULL REFERENCES tracking_sources (id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  workflow TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'other')),
  started_at TEXT NOT NULL,
  duration_s REAL,
  commit_sha TEXT NOT NULL,
  url TEXT NOT NULL,
  PRIMARY KEY (source_id, key)
);

CREATE TABLE evaluations (
  source_id TEXT NOT NULL REFERENCES tracking_sources (id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  version TEXT NOT NULL,
  run_key TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (source_id, model, version)
);

-- seq is the integer key the full-text and vector tables use as their rowid: an explicit
-- AUTOINCREMENT column stays stable across table rebuilds and VACUUM.
CREATE TABLE notes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL CHECK (type IN ('fact', 'decision', 'incident', 'runbook', 'doc')),
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  source TEXT,
  author TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX notes_type ON notes (type);

CREATE VIRTUAL TABLE notes_fts USING fts5 (title, text, content = 'notes', content_rowid = 'seq');

CREATE TRIGGER notes_fts_insert AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts (rowid, title, text) VALUES (new.seq, new.title, new.text);
END;

CREATE TRIGGER notes_fts_delete AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts (notes_fts, rowid, title, text)
  VALUES ('delete', old.seq, old.title, old.text);
END;

CREATE TRIGGER notes_fts_update AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts (notes_fts, rowid, title, text)
  VALUES ('delete', old.seq, old.title, old.text);
  INSERT INTO notes_fts (rowid, title, text) VALUES (new.seq, new.title, new.text);
END;

-- One row per note, overwritten when it is embedded again. The sqlite-vec tables are created
-- at run time by the knowledge module: they need the extension loaded.
CREATE TABLE note_embeddings (
  note_id TEXT PRIMARY KEY NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
  embedder TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  embedded_at TEXT NOT NULL
);

-- A draft is written before its approval exists, so approval_id is NULL until attached; the
-- draft is idempotent by (turn_id, tool_call_id).
CREATE TABLE proposals (
  id TEXT PRIMARY KEY NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
  agent TEXT NOT NULL,
  approval_id TEXT,
  turn_id TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  result TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE (turn_id, tool_call_id)
);

CREATE UNIQUE INDEX proposals_approval_id ON proposals (approval_id)
  WHERE approval_id IS NOT NULL;
CREATE INDEX proposals_kind_key_status ON proposals (kind, key, status);
CREATE INDEX proposals_turn_kind ON proposals (turn_id, kind);

-- The current handoff is the highest version; one version per proposal.
CREATE TABLE handoff_versions (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL UNIQUE,
  approved_at TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE TABLE kickoff (
  handoff_version INTEGER NOT NULL REFERENCES handoff_versions (version) ON DELETE CASCADE,
  agent TEXT NOT NULL,
  message_id TEXT,
  posted_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending', 'posted')),
  PRIMARY KEY (handoff_version, agent)
);

ALTER TABLE turns ADD COLUMN error_kind TEXT
  CHECK (error_kind IS NULL OR error_kind IN ('keys_exhausted'));

-- approvals gains the stall kind; a stall has no tool call, so tool_call_id, tool and
-- risk_class become nullable. SQLite cannot alter a CHECK or a NOT NULL, so the table is
-- rebuilt as 0003 did. No table references approvals, so the DROP is safe with foreign keys on.
-- Index names are global: the indexes are recreated after the rename.
CREATE TABLE approvals_new (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  tool_call_id TEXT,
  tool TEXT,
  risk_class TEXT,
  preview TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'expired')),
  note TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  task_id TEXT,
  kind TEXT NOT NULL DEFAULT 'tool' CHECK (kind IN ('tool', 'stall')),
  step INTEGER,
  proposal_kind TEXT,
  CHECK (
    (kind = 'tool' AND tool_call_id IS NOT NULL AND tool IS NOT NULL
      AND risk_class IS NOT NULL AND step IS NULL)
    OR (kind = 'stall' AND tool_call_id IS NULL AND tool IS NULL AND risk_class IS NULL
      AND step IS NOT NULL AND proposal_kind IS NULL)
  )
);

INSERT INTO approvals_new (
  id, turn_id, session_id, message_id, agent, tool_call_id, tool, risk_class, preview, status,
  note, created_at, decided_at, task_id, kind, step, proposal_kind
)
SELECT
  id, turn_id, session_id, message_id, agent, tool_call_id, tool, risk_class, preview, status,
  note, created_at, decided_at, task_id, 'tool', NULL, NULL
FROM approvals;

DROP TABLE approvals;
ALTER TABLE approvals_new RENAME TO approvals;

CREATE INDEX approvals_status ON approvals (status);
CREATE INDEX approvals_turn_id ON approvals (turn_id);
-- Unique among the pending stalls of a turn and step: a later stall at the same step, after
-- Try again decided the first, is allowed.
CREATE UNIQUE INDEX approvals_stall_step ON approvals (turn_id, step)
  WHERE kind = 'stall' AND status = 'pending';
