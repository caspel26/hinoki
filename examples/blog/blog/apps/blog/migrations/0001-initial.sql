-- hinoki migration 0001-initial
-- generated from the models; review it.

-- create table blog_publications
CREATE TABLE "blog_publications" (
    "id" UUID PRIMARY KEY,
    "name" varchar(80) NOT NULL,
    "slug" varchar(80) NOT NULL UNIQUE
);

-- create table blog_users
CREATE TABLE "blog_users" (
    "id" UUID PRIMARY KEY,
    "publication_id" UUID NOT NULL REFERENCES "blog_publications" ("id") ON DELETE RESTRICT,
    "name" varchar(80) NOT NULL,
    "token_hash" text NOT NULL UNIQUE,
    "is_editor" boolean NOT NULL,
    "username" varchar(40) UNIQUE,
    "password_hash" text
);
CREATE INDEX "ix_blog_users_publication_id_1ec4f72e" ON "blog_users" ("publication_id");

-- create table blog_posts
CREATE TABLE "blog_posts" (
    "id" UUID PRIMARY KEY,
    "publication_id" UUID NOT NULL REFERENCES "blog_publications" ("id") ON DELETE RESTRICT,
    "author_id" UUID NOT NULL REFERENCES "blog_users" ("id") ON DELETE RESTRICT,
    "title" varchar(160) NOT NULL,
    "slug" varchar(180) NOT NULL UNIQUE,
    "body" text NOT NULL,
    "published" boolean NOT NULL,
    "created_at" timestamptz NOT NULL
);
CREATE INDEX "ix_blog_posts_author_id_57194c35" ON "blog_posts" ("author_id");
CREATE INDEX "ix_blog_posts_publication_id_eb0537bb" ON "blog_posts" ("publication_id");

-- create table blog_audit
CREATE TABLE "blog_audit" (
    "id" UUID PRIMARY KEY,
    "post_id" UUID NOT NULL REFERENCES "blog_posts" ("id") ON DELETE CASCADE,
    "kind" varchar(40) NOT NULL,
    "created_at" timestamptz NOT NULL
);
CREATE INDEX "ix_blog_audit_post_id_15e04ab3" ON "blog_audit" ("post_id");
