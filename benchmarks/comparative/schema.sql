-- The same tables for every framework. No framework creates or migrates them.
DROP TABLE IF EXISTS posts;
DROP TABLE IF EXISTS authors;
CREATE TABLE authors (
    id uuid PRIMARY KEY,
    name varchar(80) NOT NULL
);
CREATE TABLE posts (
    id uuid PRIMARY KEY,
    author_id uuid NOT NULL REFERENCES authors (id),
    title varchar(120) NOT NULL,
    body text NOT NULL,
    published boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL
);
CREATE INDEX posts_published_created ON posts (published, created_at DESC, id DESC);
