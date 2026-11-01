-- Stage 2: Context budget and request limits.
ALTER TABLE models ADD COLUMN request_token_limit INTEGER
  CHECK (request_token_limit IS NULL OR request_token_limit > 0);
