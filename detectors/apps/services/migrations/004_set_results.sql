BEGIN;
SELECT pg_advisory_xact_lock(1096045135);
CREATE TABLE IF NOT EXISTS atropos_config.account_set_results (
    username text NOT NULL REFERENCES atropos_config.accounts(username) ON DELETE CASCADE,
    set_id text NOT NULL,
    payload bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (username, set_id)
);
INSERT INTO atropos_config.schema_migrations(version) VALUES (4) ON CONFLICT DO NOTHING;
COMMIT;
