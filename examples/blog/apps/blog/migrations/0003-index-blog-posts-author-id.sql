-- hinoki migration 0003-index-blog-posts-author-id
-- generated from the models; review it.

-- index blog_posts (author_id)
CREATE INDEX "ix_blog_posts_author_id" ON "blog_posts" ("author_id");
