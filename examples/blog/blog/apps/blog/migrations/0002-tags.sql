-- hinoki migration 0002-tags
-- generated from the models; review it.

-- create table blog_tags
CREATE TABLE "blog_tags" (
    "id" UUID PRIMARY KEY,
    "publication_id" UUID NOT NULL REFERENCES "blog_publications" ("id") ON DELETE RESTRICT,
    "name" varchar(40) NOT NULL,
    UNIQUE ("publication_id", "name")
);
CREATE INDEX "ix_blog_tags_publication_id_85cc512b" ON "blog_tags" ("publication_id");

-- create table blog_posts_tags
CREATE TABLE "blog_posts_tags" (
    "id" UUID PRIMARY KEY,
    "post_id" UUID NOT NULL REFERENCES "blog_posts" ("id") ON DELETE CASCADE,
    "tag_id" UUID NOT NULL REFERENCES "blog_tags" ("id") ON DELETE CASCADE,
    UNIQUE ("post_id", "tag_id")
);
CREATE INDEX "ix_blog_posts_tags_tag_id_a491a3c4" ON "blog_posts_tags" ("tag_id");
