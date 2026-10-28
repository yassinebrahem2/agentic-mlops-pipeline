-- The cooldown default must equal providers.models.DEFAULT_COOLDOWN_MINUTES (a test pins them).
ALTER TABLE providers ADD COLUMN cooldown_minutes INTEGER NOT NULL DEFAULT 15
  CHECK (cooldown_minutes > 0);

ALTER TABLE models ADD COLUMN model_type TEXT NOT NULL DEFAULT 'chat'
  CHECK (model_type IN ('chat', 'embedder'));
ALTER TABLE models ADD COLUMN dimensions INTEGER CHECK (dimensions IS NULL OR dimensions > 0);

-- Every project secret ProjectSecrets has stored, never its value. In app.db, not project.db,
-- so a removed project's refs (its project.db is deleted with its folder) can still be found.
CREATE TABLE secret_refs (
  ref TEXT PRIMARY KEY NOT NULL,
  project TEXT NOT NULL,
  created TEXT NOT NULL
);
