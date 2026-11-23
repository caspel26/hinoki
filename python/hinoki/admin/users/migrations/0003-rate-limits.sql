-- hinoki migration 0003-rate-limits
-- One portable statement for SQLite, PostgreSQL, MySQL and MariaDB.

-- create table hinoki_admin_rate_limits
CREATE TABLE hinoki_admin_rate_limits (
    bucket VARCHAR(64) NOT NULL PRIMARY KEY,
    state BIGINT NOT NULL
);

CREATE INDEX ix_hinoki_admin_rate_limits_state ON hinoki_admin_rate_limits (state);
