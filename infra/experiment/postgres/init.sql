CREATE TABLE entities (
    id text PRIMARY KEY,
    payload text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    persisted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO entities(id,payload)
SELECT 'seed-' || i, repeat('x',1024) FROM generate_series(0,999) AS i;
