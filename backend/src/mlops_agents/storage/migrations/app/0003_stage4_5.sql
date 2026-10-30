-- Stage 4-5: Model pricing columns on models (USD per million tokens).
-- All three price columns must be NULL or all three must be set.
ALTER TABLE models ADD COLUMN price_prompt_per_mtok REAL;
ALTER TABLE models ADD COLUMN price_completion_per_mtok REAL;
ALTER TABLE models ADD COLUMN price_source TEXT CHECK (
  (price_source IS NULL OR price_source IN ('provider', 'user'))
  AND (
    (price_prompt_per_mtok IS NULL AND price_completion_per_mtok IS NULL AND price_source IS NULL)
    OR (price_prompt_per_mtok IS NOT NULL AND price_completion_per_mtok IS NOT NULL AND price_source IS NOT NULL)
  )
);
