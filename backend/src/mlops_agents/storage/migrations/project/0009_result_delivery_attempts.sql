-- Result delivery attempts: retry undelivered results on observer failure (#542).
ALTER TABLE results ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0;
