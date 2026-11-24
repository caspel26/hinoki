-- Tables of the benchmark service, in the column layout Django gives the models of
-- Django Bolt's comparison app. No framework creates or migrates them.
DROP TABLE IF EXISTS articles_comment, articles_article_tags, articles_article,
    articles_tag, articles_author, benchmark_users, auth_user;
CREATE TABLE benchmark_users (
    id bigint PRIMARY KEY,
    username varchar(50) NOT NULL UNIQUE,
    email varchar(254) NOT NULL UNIQUE,
    first_name varchar(50) NOT NULL,
    last_name varchar(50) NOT NULL,
    is_active boolean NOT NULL
);
CREATE TABLE articles_author (
    id bigint PRIMARY KEY,
    name varchar(100) NOT NULL,
    email varchar(254) NOT NULL UNIQUE,
    bio text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX articles_author_name ON articles_author (name);
CREATE TABLE articles_tag (
    id bigint PRIMARY KEY,
    name varchar(50) NOT NULL UNIQUE,
    slug varchar(50) NOT NULL UNIQUE
);
CREATE TABLE articles_article (
    id bigint PRIMARY KEY,
    title varchar(200) NOT NULL,
    content text NOT NULL,
    published boolean NOT NULL,
    author_id bigint NOT NULL REFERENCES articles_author (id),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX articles_article_title ON articles_article (title);
CREATE INDEX articles_article_published ON articles_article (published);
CREATE INDEX articles_article_author ON articles_article (author_id);
CREATE INDEX articles_article_created ON articles_article (created_at);
CREATE TABLE articles_article_tags (
    id bigint PRIMARY KEY,
    article_id bigint NOT NULL REFERENCES articles_article (id),
    tag_id bigint NOT NULL REFERENCES articles_tag (id),
    UNIQUE (article_id, tag_id)
);
CREATE INDEX articles_article_tags_tag ON articles_article_tags (tag_id);
CREATE TABLE articles_comment (
    id bigint PRIMARY KEY,
    article_id bigint NOT NULL REFERENCES articles_article (id),
    author_name varchar(100) NOT NULL,
    content text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX articles_comment_article ON articles_comment (article_id);
CREATE INDEX articles_comment_created ON articles_comment (created_at);
-- Django's auth_user columns, read by Django's user loader on the Bolt service.
CREATE TABLE auth_user (
    id integer PRIMARY KEY,
    password varchar(128) NOT NULL,
    last_login timestamptz NULL,
    is_superuser boolean NOT NULL,
    username varchar(150) NOT NULL UNIQUE,
    first_name varchar(150) NOT NULL,
    last_name varchar(150) NOT NULL,
    email varchar(254) NOT NULL,
    is_staff boolean NOT NULL,
    is_active boolean NOT NULL,
    date_joined timestamptz NOT NULL
);
