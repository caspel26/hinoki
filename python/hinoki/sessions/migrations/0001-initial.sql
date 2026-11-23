-- hinoki application sessions; apply explicitly with the project migration tool.
CREATE TABLE hinoki_sessions (
    token_hash varchar(64) PRIMARY KEY,
    data text NOT NULL,
    created_at text NOT NULL,
    expires_at text NOT NULL,
    idle_expires_at text NOT NULL,
    last_seen_at text NOT NULL,
    subject varchar(255),
    revoked integer NOT NULL DEFAULT 0
);
CREATE INDEX ix_hinoki_sessions_subject ON hinoki_sessions (subject);
