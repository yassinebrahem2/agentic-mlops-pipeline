-- A key running out never fails a turn (it stalls it instead), so nothing writes why a turn
-- failed with keys_exhausted any more. The column's CHECK is its own and nothing indexes it, so
-- SQLite drops it in place.
ALTER TABLE turns DROP COLUMN error_kind;
