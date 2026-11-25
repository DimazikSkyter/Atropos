CREATE TABLE entities (id integer PRIMARY KEY, payload text NOT NULL);
INSERT INTO entities SELECT n, repeat(md5(n::text), 4) FROM generate_series(0,4095) n;
ANALYZE entities;
