BEGIN;
SELECT pg_advisory_xact_lock(1096045135);
ALTER TABLE atropos_config.metric_definitions
    ADD COLUMN IF NOT EXISTS drop_labels jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(drop_labels) = 'array'),
    ADD COLUMN IF NOT EXISTS extra_labels jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(extra_labels) = 'object');
INSERT INTO atropos_config.schema_migrations(version) VALUES (2) ON CONFLICT DO NOTHING;
COMMIT;
