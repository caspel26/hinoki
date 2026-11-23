-- hinoki migration 0001-initial
-- One portable statement for SQLite, PostgreSQL, MySQL and MariaDB.

-- create table hinoki_rate_limits
CREATE TABLE hinoki_rate_limits (
    bucket VARCHAR(64) NOT NULL PRIMARY KEY,
    state BIGINT NOT NULL
);

CREATE INDEX ix_hinoki_rate_limits_state ON hinoki_rate_limits (state);
