CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('local', 'github')),
  local_path TEXT,
  local_path_key TEXT UNIQUE,
  github_full_name TEXT,
  github_key TEXT UNIQUE,
  default_branch TEXT,
  status TEXT NOT NULL CHECK (status IN ('ok', 'missing', 'empty', 'clone_failed')),
  status_detail TEXT,
  created_at TEXT NOT NULL,
  last_opened_at TEXT,
  CHECK (
    (source = 'local' AND local_path IS NOT NULL AND local_path_key IS NOT NULL)
    OR (source = 'github' AND github_full_name IS NOT NULL AND github_key IS NOT NULL)
  )
);

CREATE TABLE providers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('hosted', 'self_hosted')),
  base_url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'needs_attention')),
  status_detail TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE provider_keys (
  provider_id TEXT NOT NULL REFERENCES providers (id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  keystore_ref TEXT NOT NULL UNIQUE,
  hint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (provider_id, position)
);

CREATE TABLE models (
  address TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL REFERENCES providers (id) ON DELETE CASCADE,
  wire_model_id TEXT NOT NULL,
  context_window INTEGER CHECK (context_window IS NULL OR context_window > 0),
  tool_calling INTEGER NOT NULL DEFAULT 0 CHECK (tool_calling IN (0, 1)),
  streaming INTEGER NOT NULL DEFAULT 0 CHECK (streaming IN (0, 1)),
  availability TEXT NOT NULL DEFAULT 'untested'
    CHECK (availability IN ('untested', 'verified', 'needs_attention')),
  availability_detail TEXT,
  verified_at TEXT,
  UNIQUE (provider_id, wire_model_id)
);

CREATE TABLE agent_model_defaults (
  agent TEXT PRIMARY KEY,
  model_address TEXT NOT NULL
);

CREATE TABLE integrations (
  name TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  login TEXT,
  verified_at TEXT
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
