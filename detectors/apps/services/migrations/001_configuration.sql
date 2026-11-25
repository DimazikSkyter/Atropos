BEGIN;
SELECT pg_advisory_xact_lock(1096045135);
CREATE SCHEMA IF NOT EXISTS atropos_config;
CREATE TABLE IF NOT EXISTS atropos_config.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS atropos_config.datasources (
    id varchar(64) PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9_-]{1,64}$'),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    name_key text NOT NULL UNIQUE,
    url text NOT NULL CHECK (url ~ '^https?://'),
    kind text NOT NULL CHECK (kind IN ('VictoriaMetrics', 'Prometheus', 'InfluxDB', 'ClickHouse')),
    token_env text NOT NULL DEFAULT '' CHECK (token_env = '' OR token_env ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    is_default boolean NOT NULL DEFAULT false,
    auth_type text NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none', 'basic', 'bearer', 'token')),
    username text NOT NULL DEFAULT '',
    password_env text NOT NULL DEFAULT '' CHECK (password_env = '' OR password_env ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    "database" text NOT NULL DEFAULT '',
    organization text NOT NULL DEFAULT '',
    bucket text NOT NULL DEFAULT '',
    influx_version text NOT NULL DEFAULT '2' CHECK (influx_version IN ('1', '2', '3')),
    timeout_seconds double precision NOT NULL DEFAULT 20 CHECK (timeout_seconds BETWEEN 1 AND 300),
    tls_verify boolean NOT NULL DEFAULT true,
    min_interval_seconds integer NOT NULL DEFAULT 15 CHECK (min_interval_seconds BETWEEN 1 AND 86400),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (auth_type = 'none' AND username = '' AND password_env = '' AND token_env = '') OR
        (auth_type = 'basic' AND username <> '' AND password_env <> '' AND token_env = '') OR
        (auth_type IN ('bearer', 'token') AND token_env <> '' AND username = '' AND password_env = '')
    ),
    CHECK (
        (kind IN ('VictoriaMetrics', 'Prometheus') AND auth_type IN ('none', 'basic', 'bearer')) OR
        (kind = 'ClickHouse' AND auth_type IN ('none', 'basic') AND "database" <> '') OR
        (kind = 'InfluxDB' AND (
            (influx_version = '1' AND "database" <> '' AND auth_type IN ('none', 'basic')) OR
            (influx_version = '2' AND organization <> '' AND bucket <> '' AND auth_type IN ('none', 'token')) OR
            (influx_version = '3' AND "database" <> '' AND auth_type IN ('none', 'token'))
        ))
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS datasources_one_default
    ON atropos_config.datasources ((true)) WHERE is_default;

CREATE TABLE IF NOT EXISTS atropos_config.metric_definitions (
    name text PRIMARY KEY CHECK (name ~ '^[a-zA-Z_:][a-zA-Z0-9_:]*$'),
    datasource_id varchar(64) NOT NULL REFERENCES atropos_config.datasources(id) ON DELETE RESTRICT,
    query text NOT NULL CHECK (length(btrim(query)) > 0),
    description text NOT NULL DEFAULT '',
    interval_seconds integer NOT NULL DEFAULT 30 CHECK (interval_seconds BETWEEN 1 AND 86400),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS metric_definitions_datasource ON atropos_config.metric_definitions(datasource_id);

CREATE TABLE IF NOT EXISTS atropos_config.service_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    server_url text NOT NULL CHECK (server_url ~ '^https?://'),
    server_token_env text NOT NULL DEFAULT '' CHECK (
        server_token_env = '' OR server_token_env ~ '^[A-Za-z_][A-Za-z0-9_]*$'
    ),
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO atropos_config.service_settings(singleton, server_url)
    VALUES (true, 'http://localhost:8000') ON CONFLICT DO NOTHING;
INSERT INTO atropos_config.schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;
COMMIT;
