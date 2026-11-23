-- hinoki shared cache; apply explicitly with the project migration tool.
CREATE TABLE hinoki_cache (
    cache_key varchar(256) PRIMARY KEY,
    value_hex text NOT NULL,
    expires_at text NOT NULL
);
CREATE INDEX ix_hinoki_cache_expires_at ON hinoki_cache (expires_at);
