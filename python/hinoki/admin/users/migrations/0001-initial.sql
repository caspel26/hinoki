-- hinoki migration 0001-initial
-- generated from the models; review it.

-- create table hinoki_admin_users
CREATE TABLE "hinoki_admin_users" (
    "id" UUID PRIMARY KEY,
    "username" varchar(150) NOT NULL UNIQUE,
    "password_hash" varchar(255) NOT NULL,
    "is_active" boolean NOT NULL,
    "is_superuser" boolean NOT NULL,
    "created_at" timestamptz NOT NULL,
    "last_login" timestamptz
);

-- create table hinoki_admin_sessions
CREATE TABLE "hinoki_admin_sessions" (
    "id" UUID PRIMARY KEY,
    "token_hash" varchar(64) NOT NULL UNIQUE,
    "user_id" UUID NOT NULL REFERENCES "hinoki_admin_users" ("id") ON DELETE CASCADE,
    "created_at" timestamptz NOT NULL,
    "expires_at" timestamptz NOT NULL,
    "last_seen_at" timestamptz NOT NULL,
    "user_agent" varchar(200) NOT NULL,
    "fingerprint" varchar(64) NOT NULL
);
CREATE INDEX "ix_hinoki_admin_sessions_user_id" ON "hinoki_admin_sessions" ("user_id");

-- create table hinoki_admin_grants
CREATE TABLE "hinoki_admin_grants" (
    "id" UUID PRIMARY KEY,
    "user_id" UUID NOT NULL REFERENCES "hinoki_admin_users" ("id") ON DELETE CASCADE,
    "model" varchar(80) NOT NULL,
    "action" varchar(20) NOT NULL,
    UNIQUE ("user_id", "model", "action")
);
CREATE INDEX "ix_hinoki_admin_grants_user_id" ON "hinoki_admin_grants" ("user_id");
