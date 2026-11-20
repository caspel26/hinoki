-- hinoki migration 0002-login-windows
-- generated from the models; review it.

-- create table hinoki_admin_login_windows
CREATE TABLE "hinoki_admin_login_windows" (
    "key" varchar(64) PRIMARY KEY,
    "started_at" timestamptz NOT NULL,
    "count" bigint NOT NULL
);
CREATE INDEX "ix_hinoki_admin_login_windows_started_at" ON "hinoki_admin_login_windows" ("started_at");
