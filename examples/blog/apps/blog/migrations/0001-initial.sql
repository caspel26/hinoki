-- hinoki migration 0001-initial
-- generated from the models; review it.

-- create table blog_users
CREATE TABLE "blog_users" (
    "id" uuid PRIMARY KEY,
    "name" varchar(80) NOT NULL,
    "token" text NOT NULL UNIQUE,
    "is_editor" boolean NOT NULL
);

-- create table blog_posts
CREATE TABLE "blog_posts" (
    "id" uuid PRIMARY KEY,
    "author_id" uuid NOT NULL REFERENCES "blog_users" ("id") ON DELETE RESTRICT,
    "title" varchar(160) NOT NULL,
    "slug" varchar(180) NOT NULL UNIQUE,
    "body" text NOT NULL,
    "published" boolean NOT NULL,
    "created_at" timestamptz NOT NULL
);
