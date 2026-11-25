BEGIN;
SELECT pg_advisory_xact_lock(1096045135);
CREATE TABLE IF NOT EXISTS atropos_config.accounts (
    username text PRIMARY KEY CHECK (length(username) BETWEEN 1 AND 80),
    password_hash text NOT NULL,
    workspace jsonb NOT NULL DEFAULT '{}',
    revision bigint NOT NULL DEFAULT 1,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS atropos_config.account_results (
    username text PRIMARY KEY REFERENCES atropos_config.accounts(username) ON DELETE CASCADE,
    revision bigint NOT NULL,
    payload bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO atropos_config.schema_migrations(version) VALUES (3) ON CONFLICT DO NOTHING;
COMMIT;
